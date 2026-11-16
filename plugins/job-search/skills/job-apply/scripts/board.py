#!/usr/bin/env python3
"""Короткие сводки для команд бота (читаются с телефона, без ссылок и простыней).

  board.py queue      /queue    — очередь автооткликов по статусам: что ждёт и почему
  board.py tracker    /tracker  — открытые строки job_tracker: что требует действия, что ждёт ответа
  board.py contacts   /contacts — контакты рекрутеров: кто, где, писал ли
"""
import argparse, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import status as S  # noqa: E402

QUEUE_ORDER = [("paused", "⏸ ждут тебя"), ("manual", "✍ заполнить руками"), ("in-progress", "▶ в работе"),
               ("new", "🆕 в очереди"), ("failed", "⚠ ошибка"), ("bl-review", "🔎 проверка")]


def short(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def reason(note):
    """Заметка очереди -> одна короткая причина: без ссылок, длинные значения в «…» схлопнуты."""
    if re.match(r"[\s—-]*только hh", note or ""):
        return "есть только на hh.ru — воркер туда не подаёт"
    n = re.sub(r"https?://\S+", "", note or "")
    n = re.sub(r"«[^»]{30,}»?", "«…»", n)
    n = re.sub(r"(\s*\|\s*)+", " | ", n).strip(" |")
    n = re.sub(r"^[\s—-]+", "", n)
    n = re.sub(r"\s*\([^)]{0,12}$", "", n)                       # обрезанный хвост «(Нап»
    return short(n.split("\n")[0], 90)


def cmd_queue(a):
    rows = S.read_tsv(S.HOME / "queue.tsv", header=True)
    out = []
    for st, title in QUEUE_ORDER:
        items = [r for r in rows if r["status"] == st]
        if not items:
            continue
        out.append(f"{title} ({len(items)})")
        for r in items:
            why = reason(r.get("note", "")) if st != "new" else ""
            out.append(f"  {r['key']} {short(r['title'], 55)}" + (f"\n      {why}" if why else ""))
    print("\n".join(out) or "Очередь пуста.")


def sheet():
    """Строки job_tracker и откуда они: (rows, «из таблицы, 01:50» | «кэш на 01:40 — таблица не ответила»)."""
    rows, ts = S.cached_sheet()
    if rows is not None and time.time() - ts < 120:               # только что читали — не дёргаем таблицу
        return rows, f"из таблицы, {time.strftime('%H:%M', time.localtime(ts))}"
    try:
        fresh = S.fetch_sheet()
        if fresh:
            return fresh, f"из таблицы, {time.strftime('%H:%M')}"
    except Exception:
        pass
    when = time.strftime("%d.%m %H:%M", time.localtime(ts)) if rows else "—"
    return rows or [], f"⚠ кэш на {when} — таблица не ответила"


def esc(s):
    import html
    return html.escape(s or "", quote=False)


def clip(s, n):
    """Не рубить на полуслове: до n символов, по концу предложения (или слова)."""
    s = re.sub(r"\s+", " ", s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    end = max(cut.rfind(". "), cut.rfind("; "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > n // 2 else cut[:cut.rfind(" ")]) + " …"


def card(r):
    """Вакансия: заголовок строкой + сворачиваемая цитата с подробностями (tap — раскрыть)."""
    v = r["values"] + [""] * 14
    company, title, date, stage, status, nxt, _, notes, url = v[:9]
    mark = "⏰" if due(nxt) else "•"
    head = f"{mark} <b>#{r['row']} {esc(company)}</b> — {esc(title)}"
    body = [f"{esc(stage or 'Отклик')} {esc(date[8:10] + '.' + date[5:7] if re.match(r'\d{4}-\d{2}-\d{2}', date) else date)}"
            f" · {esc(status)}"]
    if nxt and nxt != "-":
        body.append(f"→ {esc(clip(nxt, 400))}")
    if notes:
        body.append(esc(clip(notes, 500)))
    link = next((u for u in re.split(r"\s*;\s*|\s+", url) if u.startswith("http")), "")
    if link:
        body.append(f'<a href="{esc(link)}">вакансия</a>')
    return head + "\n<blockquote expandable>" + "\n".join(body) + "</blockquote>"


def cmd_tracker(a):
    data, src = sheet()
    rows = [r for r in data if len(r["values"]) > 4 and r["values"][0] and r["values"][4]]
    opened = [r for r in rows if r["values"][4] != "Закрыто"]
    groups = {}
    for r in opened:
        groups.setdefault(r["values"][4], []).append(r)
    order = sorted(groups, key=lambda s: (0 if "ДЕЙСТВ" in s.upper() else 1 if s == "В процессе" else 2, s))
    ndue = sum(due((r["values"] + [""] * 6)[5]) for r in opened)
    head = (f"<b>Трекер</b>: открыто {len(opened)}, закрыто {len(rows) - len(opened)}, ⏰ пора действовать {ndue}\n"
            f"<i>{esc(src)}. Нажми на цитату — раскроется.</i>")
    if not a.tg:                                                   # консоль: коротко, без HTML
        print(re.sub(r"<[^>]+>", "", head))
        for st in order:
            print(f"\n{st} ({len(groups[st])})")
            for r in groups[st]:
                v = r["values"] + [""] * 6
                print(f"  {'⏰' if due(v[5]) else '#'}{r['row']} {short(v[0], 28)} — {short(v[1], 40)}")
        return
    # Telegram: сообщения ≤ 3800 символов, по статусам; ⏰ — первыми внутри статуса
    msgs, cur = [], head
    for st in order:
        items = sorted(groups[st], key=lambda r: (not due((r["values"] + [""] * 6)[5]), r["row"]))
        block = [f"\n\n<b>{esc(st)} ({len(items)})</b>"] + ["\n" + card(r) for r in items]
        for piece in block:
            if len(cur) + len(piece) > 3800:
                msgs.append(cur)
                cur = piece.lstrip("\n")
            else:
                cur += piece
    msgs.append(cur)
    for m in msgs:
        p = subprocess.run(["tg", "send", "--html", "--stdin"], input=m, capture_output=True, text=True)
        if p.returncode:
            print(f"ERR tg send: {p.stderr.strip()[:300]}")
            return
    print(f"Трекер — {len(msgs)} сообщ. выше.")


def due(f):
    """«След. действие» начинается с даты «дд.мм» / «~дд–дд.мм», и она уже наступила."""
    m = re.match(r"\s*~?(\d{1,2})(?:[–-]\d{1,2})?\.(\d{2})", f or "")
    if not m:
        return False
    today = time.localtime()
    return (int(m.group(2)), int(m.group(1))) <= (today.tm_mon, today.tm_mday)


def cmd_contacts(a):
    sys.path.insert(0, str(HERE.parent.parent / "job-contacts" / "scripts"))
    import contacts as C
    rows = C.read_tsv(C.CONTACTS, C.C_FIELDS)
    if not rows:
        print("Контактов нет."); return
    mark = {"new": "·", "contacted": "✉", "replied": "💬", "bounced": "✖", "skip": "–"}
    by = {}
    for r in rows:
        by.setdefault(r["company"], []).append(r)
    out = [f"Контакты: {len(rows)} в {len(by)} компаниях  (· не писал, ✉ писал, 💬 ответил)"]
    for comp, people in by.items():
        out.append(f"\n{comp}")
        for r in people:
            d = r["last_contact_at"]
            last = f", {d[8:10]}.{d[5:7]}" if len(d) >= 10 else ""
            out.append(f"  {mark.get(r['status'], '?')} {short(r['name'], 28)} ({r['role']}) {r['value']}{last}")
    print("\n".join(out))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name, f in (("queue", cmd_queue), ("tracker", cmd_tracker), ("contacts", cmd_contacts)):
        sp.add_parser(name).set_defaults(f=f)
    sp.choices["tracker"].add_argument("--tg", action="store_true", help="в Telegram: HTML, сворачиваемые карточки")
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
