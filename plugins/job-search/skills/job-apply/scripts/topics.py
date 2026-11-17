#!/usr/bin/env python3
"""Чистка вкладок Telegram-бота (tg-bridge, Threaded Mode): остаются только вакансии, открытые в трекере.

Вкладка автоотклика помечена kind="apply:<ключ очереди>" (threads.json). Её судьба — по трекеру:
  open      строка job_tracker найдена и открыта (не «Закрыто»)                -> оставить
  running   отклик идёт прямо сейчас (pipeline.py <ключ> жив) — вкладка нужна ему -> оставить
  closed    строка трекера «Закрыто»                                            -> удалить
  queue     в трекере нет: в очереди new/failed/paused/skipped/manual…            -> удалить
            (worker откроет вкладку заново, когда возьмёт вакансию)
  unknown   отклик был, но строку в трекере не сопоставить                      -> удалить, в списке отдельно
  other     не автоотклик: задачи агенту, тесты; оставить, только если задача сейчас выполняется
Строка трекера ищется по ссылке из applications/q<ключ>-*/tracker-row.json (столбец I), затем по компании
и должности из tracker-row.json или из заметки очереди («— Компания Должность»).

  topics.py list               таблица вкладок и решение по каждой
  topics.py clean [--dry-run]  удалить всё, кроме open/running (tg topic rm)
  topics.py cleanup            для бота /cleanup: список + кнопка «Удалить N» (action topics-clean)
  topics.py clean-go           то, что жмёт кнопка: удалить и коротко ответить
"""
import argparse, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import status as S  # noqa: E402  (norm_url, norm_txt, read_tsv, fetch_sheet, cached_sheet)

HOME = S.HOME
TG_DATA = Path(os.environ.get("TG_BRIDGE_DATA", "~/.local/share/tg-bridge")).expanduser()
QUEUE_NOT_SENT = {"skipped", "bl-done"}
KEEP = ("open", "running")                                      # остальное /cleanup удаляет


def tg_bin():
    return shutil.which("tg") or str(Path("~/.local/bin/tg").expanduser())


def sheet_rows(max_age=900):
    rows, ts = S.cached_sheet() if hasattr(S, "cached_sheet") else (None, 0)
    if rows is None or time.time() - (ts or 0) > max_age:
        try:
            rows = S.fetch_sheet() or rows
        except Exception as e:                                     # нет сети — работаем по кэшу
            print(f"WARN: лист не обновлён ({e}); беру кэш", file=sys.stderr)
    return rows or []


def queue_rows():
    return {r["key"]: r for r in S.read_tsv(HOME / "queue.tsv", header=True)}


def app_info(key):
    """tracker-row.json из папки автоотклика: {A,B,I,…} или {}."""
    for d in sorted((HOME / "applications").glob(f"q{key.replace('.', '-')}-*")):
        f = d / "tracker-row.json"
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
    return {}


def find_row(key, q, rows):
    info = app_info(key)
    urls = [S.norm_url(u) for u in re.split(r"\s*;\s*|\s+", info.get("I", "")) if u.startswith("http")]
    for r in rows:
        cell = r["values"][8] if len(r["values"]) > 8 else ""
        if urls and any(S.norm_url(u) in urls for u in re.split(r"\s*;\s*|\s+", cell) if u):
            return r
    # компания + должность: из tracker-row.json, иначе из заметки очереди «— Компания Должность»
    cands = []
    if info.get("A") and info.get("B"):
        cands.append((info["A"], info["B"]))
    note = (q or {}).get("note", "")
    qtitle = re.split(r"\s*\|\s*", (q or {}).get("title", ""))[0]
    if not re.search(r"только hh|разведка|not_found|нет ответа", note):
        m = re.search(r"—\s*([^|]+?)(?:\s*\||$)", note)            # «— Компания Должность»: компания 1–3 слова
        if m:
            w = m.group(1).split()
            cands += [(" ".join(w[:k]), " ".join(w[k:])) for k in (1, 2, 3) if len(w) > k]
        m = re.match(r"\s*([^:—]{2,40}):", note)                     # «Компания: отклик на hh …» + должность из очереди
        if m and qtitle:
            cands.append((m.group(1), qtitle))
    qt = (q or {}).get("title", "")
    m = re.match(r"^[^\w«\"(]*(.+?)\s+в\s+(.+?)\s*$", qt)          # «Должность в Компания»
    if m:
        cands.append((re.sub(r"^(ПАО|ООО|АО)\s+|[«»\"]", "", m.group(2)), m.group(1)))
    for company, title in cands:
        c, t = S.norm_txt(company)[:4], S.norm_txt(title)[:25]
        for r in rows:
            v = r["values"]
            if c and t and c in S.norm_txt(v[0]) and t in S.norm_txt(v[1]):
                return r
    return None


def running_keys():
    p = subprocess.run(["pgrep", "-af", r"pipeline\.py [0-9]"], capture_output=True, text=True)
    return set(re.findall(r"pipeline\.py ([0-9.]+)", p.stdout))


def busy_threads():
    """Вкладки, где сейчас выполняется задача агенту (tg-bridge running/<вкладка>)."""
    d = TG_DATA / "running"
    return {f.name.split(".")[0] for f in d.iterdir()} if d.exists() else set()


def decide():
    rows, queue, live, busy = sheet_rows(), queue_rows(), running_keys(), busy_threads()
    th = json.loads((TG_DATA / "threads.json").read_text()) if (TG_DATA / "threads.json").exists() else {}
    out = []
    for tid, t in th.items():
        kind = t.get("kind", "")
        if kind == "control":                                       # «Пульт» tg-bridge — не трогаем
            out.append(dict(tid=tid, key="", name=t.get("name", ""), verdict="running", why="пульт бота"))
            continue
        if not kind.startswith("apply:"):
            verdict = "running" if tid in busy else "other"
            out.append(dict(tid=tid, key="", name=t.get("name", ""), verdict=verdict,
                            why=f"{kind or 'вкладка'}: " + ("задача выполняется" if tid in busy else "не вакансия")))
            continue
        key = kind.split(":", 1)[1]
        q = queue.get(key)
        r = find_row(key, q, rows)
        qs = (q or {}).get("status", "")
        if key in live:
            verdict, why = "running", "отклик идёт сейчас"
        elif r:
            e = r["values"][4] if len(r["values"]) > 4 else ""
            verdict = "closed" if e == "Закрыто" else "open"
            why = f"трекер #{r['row']}: {e or '—'}"
        elif qs and qs not in QUEUE_NOT_SENT | {"done"}:
            verdict, why = "queue", f"очередь: {qs}, в трекере нет"
        elif qs in QUEUE_NOT_SENT:
            verdict, why = "queue", f"очередь: {qs}, не подавали"
        else:
            verdict, why = "unknown", f"очередь: {qs or 'нет записи'}, строка трекера не найдена"
        out.append(dict(tid=tid, key=key, name=t.get("name", ""), verdict=verdict, why=why))
    return out


EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u20E3]+")


def fmt(items):
    """Названия вкладок — из постов каналов, там бывают эмодзи («5️⃣ аналитик…»); в списке бота их не надо."""
    return "\n".join(f"{re.sub(r'^[\s\d]*(?=\s)', '', EMOJI.sub('', i['name'])).strip()[:60]} — {i['why']}" for i in items)


def cmd_list(a):
    items = decide()
    print(fmt(items) or "вкладок автооткликов нет")


def remove(items):
    ids = [i["tid"] for i in items]
    if not ids:
        return 0, "удалять нечего"
    p = subprocess.run([tg_bin(), "topic", "rm", *ids], capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def gone(items):
    return [i for i in items if i["verdict"] not in KEEP]


def cmd_clean(a):
    items = gone(decide())
    if a.dry_run:
        print(fmt(items) or "удалять нечего")
        return
    rc, out = remove(items)
    print(out)
    sys.exit(rc)


def cmd_cleanup(a):
    """/cleanup: показать, что удалится, и кнопку подтверждения (ничего не удаляет сам)."""
    items = decide()
    g, keep = gone(items), [i for i in items if i["verdict"] in KEEP]
    if not g:
        print(f"Удалять нечего: все {len(keep)} вкладок — открытые в трекере или в работе.")
        return
    unk = [i for i in g if i["verdict"] == "unknown"]
    text = f"Удалю вкладки ({len(g)}) вместе с перепиской в них:\n{fmt([i for i in g if i['verdict'] != 'unknown'])}"
    if unk:
        text += f"\n\nОтклик был, но в трекере строку не нашёл — тоже удалю ({len(unk)}):\n{fmt(unk)}"
    text += f"\n\nОставлю ({len(keep)}): открытые в трекере и идущие сейчас."
    p = subprocess.run([tg_bin(), "send", text[:3900], "--button", f"Удалить {len(g)}=topics-clean:go",
                        "--cols", "1"], capture_output=True, text=True)
    print("Список и кнопка — следующим сообщением." if p.returncode == 0 else f"ERR tg send: {p.stderr.strip()}")


def cmd_clean_go(a):
    items = gone(decide())                                         # пересчёт на момент нажатия
    rc, out = remove(items)
    n = sum(1 for l in out.splitlines() if l.startswith(("удалена", "не найдена")))
    print(f"Удалено вкладок: {n}" if rc == 0 else f"Часть не удалилась:\n{out[-800:]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("list").set_defaults(f=cmd_list)
    c = sp.add_parser("clean"); c.add_argument("--dry-run", action="store_true"); c.set_defaults(f=cmd_clean)
    sp.add_parser("cleanup").set_defaults(f=cmd_cleanup)
    g = sp.add_parser("clean-go"); g.add_argument("arg", nargs="?"); g.set_defaults(f=cmd_clean_go)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
