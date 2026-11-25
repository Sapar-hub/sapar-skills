#!/usr/bin/env python3
"""Сверка: что реально ушло, что застряло, что пора пнуть. Источник правды по отправке — applied.tsv,
по воронке — job_tracker (через Apps Script web app, sheet.py).

  status.py            полный отчёт (тянет лист; ~3–10 с)
  status.py --hook     одна строка-сводка, только локальные файлы + кэш листа (быстро; хук её больше не вызывает);
                       кэш старше часа обновляется в фоне

Разделы отчёта:
  1. НЕТ В ТРЕКЕРЕ   — строка applied.tsv, которой нет в листе (по ссылке I или дате+должности)
  2. ОЧЕРЕДЬ         — queue.tsv: new / running / paused / failed (не отправлено!)
  3. БЕЗ ИТОГА       — папка applications/ с fill-plan, но без applied.tsv и без закрытой записи в очереди
  4. ПОРА ДЕЙСТВОВАТЬ — открытые строки листа, где дата в F (след. действие) уже наступила
  5. ТИШИНА          — открытые строки старше 14 дней без даты в F
"""
import argparse, csv, json, os, re, subprocess, sys, time
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("JOB_APPLY_HOME", "~/.local/share/job-apply")).expanduser()
CACHE = HOME / "sheet-cache.json"
OPEN = {"Ожидание ответа", "В процессе"}
QUEUE_OPEN = {"new", "running", "manual", "paused", "failed"}


def norm_url(u):
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://(www\.)?", "", u)
    u = re.sub(r"[?#].*$", "", u)
    return u.rstrip("/")


def norm_txt(s):
    return re.sub(r"[^0-9a-zа-яё]+", " ", (s or "").lower()).strip()


def fetch_sheet():
    sys.path.insert(0, str(HERE))
    import sheet
    if not sheet.conf():
        return None
    rows = sheet.call("find", query="", limit=1000)["rows"]
    CACHE.write_text(json.dumps({"ts": time.time(), "rows": rows}, ensure_ascii=False), encoding="utf-8")
    return rows


def cached_sheet():
    try:
        c = json.loads(CACHE.read_text(encoding="utf-8"))
        return c["rows"], c["ts"]
    except Exception:
        return None, 0


def read_tsv(p, header=False):
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as fh:
        r = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        rows = [x for x in r if x and any(x)]
    if header and rows:
        h = rows[0]
        return [dict(zip(h, x)) for x in rows[1:]]
    return rows


def in_sheet(app, rows):
    d, company, title, url = app[0], app[1], app[2], app[3] if len(app) > 3 else ""
    u = norm_url(url)
    t = norm_txt(title)[:25]
    for r in rows:
        v = r["values"]
        if u and norm_url(v[8]) == u:
            return True
        if t and t in norm_txt(v[1]) and norm_txt(company)[:4] in norm_txt(v[0]):
            return True
    return False


def due_date(f, today):
    """первая дата dd.mm в F → date (год текущий; если ушла на >6 мес назад — следующий)"""
    m = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", f or "")
    if not m:
        return None
    y = int(m.group(3) or today.year)
    try:
        d = date(y, int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    if not m.group(3) and (today - d).days > 180:
        d = date(y + 1, d.month, d.day)
    return d


def parse_c(c):
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(c.strip(), fmt).date()
        except ValueError:
            pass
    return None


def report(rows):
    today = date.today()
    applied = read_tsv(HOME / "applied.tsv")
    queue = read_tsv(HOME / "queue.tsv", header=True)
    out = {}

    out["missing"] = [a for a in applied if len(a) >= 3] if rows is None else \
        [a for a in applied if len(a) >= 3 and not in_sheet(a, rows)]
    out["queue"] = [q for q in queue if q.get("status") in QUEUE_OPEN]

    applied_dirs = {(a[4].split("/")[0] if len(a) > 4 else "") for a in applied}
    applied_dirs |= {Path(a[4]).parts[1] for a in applied if len(a) > 4 and a[4].startswith("applications/")}
    closed_keys = {q["key"] for q in queue if q.get("status") in {"done", "skipped"}}
    open_keys = {q["key"] for q in out["queue"]}
    orphans = []
    for d in sorted((HOME / "applications").iterdir()) if (HOME / "applications").exists() else []:
        if not d.is_dir() or d.name.startswith("_") or d.name in applied_dirs:
            continue
        m = re.match(r"q(\d+)-(\d+)-", d.name)
        key = f"{m.group(1)}.{m.group(2)}" if m else None
        if key and (key in closed_keys or key in open_keys):
            continue
        if (d / "fill-plan.json").exists() or list(d.glob("*.pdf")):
            orphans.append(d.name)
    out["orphans"] = orphans

    due, stale = [], []
    for r in rows or []:
        v = r["values"]
        if v[4] not in OPEN:
            continue
        dd = due_date(v[5], today)
        if dd and dd <= today:
            due.append((r["row"], v, dd))
        elif not dd:
            c = parse_c(v[2])
            if c and (today - c).days > 14:
                stale.append((r["row"], v, (today - c).days))
    out["due"], out["stale"] = due, stale
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hook", action="store_true")
    a = ap.parse_args()

    if a.hook:
        rows, ts = cached_sheet()
        if time.time() - ts > 3600:
            subprocess.Popen([sys.executable, "-I", "-c",
                              f"import sys; sys.path.insert(0, {str(HERE)!r}); import status; status.fetch_sheet()"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        if rows is None:
            return
        r = report(rows)
        parts = []
        if r["missing"]: parts.append(f"{len(r['missing'])} отправленных нет в трекере")
        if r["queue"]: parts.append(f"{len(r['queue'])} в очереди не отправлено")
        if r["orphans"]: parts.append(f"{len(r['orphans'])} откликов без итога")
        if r["due"]: parts.append(f"{len(r['due'])} строк с наступившим след. действием")
        if parts:
            print("job-apply: " + ", ".join(parts) + ". Подробно: python3 -I <job-apply>/scripts/status.py")
        return

    try:
        rows = fetch_sheet()
    except (Exception, SystemExit) as e:
        print(f"WARN: лист недоступен ({e}), беру кэш", file=sys.stderr)
        rows, _ = cached_sheet()
    if rows is None:
        print("WARN: листа нет (sheet.env не настроен и кэша нет) — раздел 1 = весь applied.tsv", file=sys.stderr)
    r = report(rows)

    def sec(title, items, fmt):
        print(f"\n{title}: {len(items)}")
        for i in items:
            print("  " + fmt(i))

    sec("1. НЕТ В ТРЕКЕРЕ (отправлено по applied.tsv)", r["missing"],
        lambda a: f"{a[0]} {a[1]} — {a[2]} [{a[5] if len(a) > 5 else ''}] {a[3] if len(a) > 3 else ''}")
    sec("2. ОЧЕРЕДЬ (НЕ отправлено)", r["queue"],
        lambda q: f"{q['key']} {q['status']}: {q['title'][:70]} — {q.get('note', '').strip(' —')[:120]}")
    sec("3. БЕЗ ИТОГА (папка есть, в applied.tsv/очереди нет)", r["orphans"], str)
    sec("4. ПОРА ДЕЙСТВОВАТЬ", r["due"], lambda x: f"#{x[0]} {x[1][0]} — {x[1][1][:60]}: {x[1][5][:120]}")
    sec("5. ТИШИНА >14 дн. без плана в F", r["stale"], lambda x: f"#{x[0]} {x[1][0]} — {x[1][1][:60]} ({x[2]} дн.)")


if __name__ == "__main__":
    main()
