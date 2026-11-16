#!/usr/bin/env python3
"""Сводки для команд бота: читаются с телефона, без простыней адресов и без эмодзи — только жирный/курсив.

  board.py queue [--tg]      /queue    — очередь автооткликов по статусам: что ждёт и почему
  board.py tracker [--tg]    /tracker  — открытые строки job_tracker карточками (подробности свёрнуты)
  board.py contacts [--tg]   /contacts — контакты рекрутеров: кто, где, писал ли

--tg: HTML прямо в Telegram (`tg send --html`, в вкладку команды), режется по сообщениям под лимит.
Без --tg — короткий текст для консоли.
"""
import argparse, html, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import status as S  # noqa: E402

QUEUE_ORDER = [("paused", "Ждут тебя"), ("manual", "Заполнить руками"), ("in-progress", "В работе"),
               ("new", "В очереди"), ("failed", "Ошибка"), ("bl-review", "Проверка")]
CONTACT_STATUS = {"new": "не писал", "contacted": "писал", "replied": "ответил", "bounced": "адрес не работает",
                  "skip": "пропуск"}
LIMIT = 3800


def esc(s):
    return html.escape(s or "", quote=False)


def short(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def clip(s, n):
    """Не рубить на полуслове: до n символов, по концу предложения (или слова)."""
    s = re.sub(r"\s+", " ", s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    end = max(cut.rfind(". "), cut.rfind("; "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:end + 1] if end > n // 2 else cut[:cut.rfind(" ")]) + " …"


def ddmm(d):
    return f"{d[8:10]}.{d[5:7]}" if re.match(r"\d{4}-\d{2}-\d{2}", d or "") else (d or "")


def emit(a, pieces):
    """pieces: HTML-куски (заголовок, блоки). --tg: склеить в сообщения ≤ LIMIT и отправить; иначе — текст."""
    if not a.tg:
        print(re.sub(r"<[^>]+>", "", "".join(pieces)).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
        return
    msgs, cur = [], ""
    for p in pieces:
        if cur and len(cur) + len(p) > LIMIT:
            msgs.append(cur)
            cur = p.lstrip("\n")
        else:
            cur += p
    msgs.append(cur)
    for m in msgs:
        r = subprocess.run(["tg", "send", "--html", "--stdin"], input=m, capture_output=True, text=True)
        if r.returncode:
            print(f"ERR tg send: {r.stderr.strip()[:300]}")
            return
    # бот не отвечает на пустой вывод команды — сводка уже отправлена


# --- /queue ---

def reason(note):
    """Заметка очереди -> одна короткая причина: без ссылок, длинные значения в «…» схлопнуты."""
    if re.match(r"[\s—-]*только hh", note or ""):
        return "есть только на hh.ru — воркер туда не подаёт"
    n = re.sub(r"https?://\S+", "", note or "")
    n = re.sub(r"«[^»]{30,}»?", "«…»", n)
    n = re.sub(r"(\s*\|\s*)+", " | ", n).strip(" |")
    n = re.sub(r"^[\s—-]+", "", n)
    n = re.sub(r"\s*\([^)]{0,12}$", "", n)                       # обрезанный хвост «(Нап»
    return clip(n.split("\n")[0], 160)


def cmd_queue(a):
    rows = S.read_tsv(S.HOME / "queue.tsv", header=True)
    pieces = []
    for st, title in QUEUE_ORDER:
        items = [r for r in rows if r["status"] == st]
        if not items:
            continue
        pieces.append(f"\n\n<b>{title}</b> ({len(items)})")
        for r in items:
            link = r.get("href") or r.get("post") or ""
            name = f'<a href="{esc(link)}">{esc(short(r["title"], 70))}</a>' if link.startswith("http") else esc(short(r["title"], 70))
            why = reason(r.get("note", "")) if st != "new" else ""
            pieces.append(f"\n<code>{r['key']}</code> {name}" + (f"\n<i>{esc(why)}</i>" if why else ""))
    emit(a, ["<b>Очередь автооткликов</b>"] + pieces if pieces else ["Очередь пуста."])


# --- /tracker ---

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
    return rows or [], f"КЭШ на {when}: таблица не ответила"


def due(f):
    """«След. действие» начинается с даты «дд.мм» / «~дд–дд.мм», и она уже наступила."""
    m = re.match(r"\s*~?(\d{1,2})(?:[–-]\d{1,2})?\.(\d{2})", f or "")
    if not m:
        return False
    today = time.localtime()
    return (int(m.group(2)), int(m.group(1))) <= (today.tm_mon, today.tm_mday)


def card(r):
    """Вакансия: заголовок строкой + сворачиваемая цитата с подробностями (tap — раскрыть)."""
    v = r["values"] + [""] * 14
    company, title, date, stage, status, nxt, _, notes, url = v[:9]
    head = f"<b>#{r['row']} {esc(company)}</b> — {esc(title)}" + (" · <b><i>пора</i></b>" if due(nxt) else "")
    body = [f"<i>{esc(stage or 'Отклик')} {esc(ddmm(date))} · {esc(status)}</i>"]
    if nxt and nxt != "-":
        body.append(f"<b>Дальше:</b> {esc(clip(nxt, 400))}")
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
    pieces = [f"<b>Трекер</b>: открыто {len(opened)}, закрыто {len(rows) - len(opened)}, "
              f"пора действовать — {ndue}\n<i>{esc(src)}. Подробности — нажать на цитату.</i>"]
    for st in order:
        items = sorted(groups[st], key=lambda r: (not due((r["values"] + [""] * 6)[5]), r["row"]))
        pieces.append(f"\n\n<b>{esc(st)}</b> ({len(items)})")
        pieces += ["\n" + (card(r) if a.tg else f"#{r['row']} {esc(short(r['values'][0], 28))} — "
                                                  f"{esc(short(r['values'][1], 40))}"
                                                  + (" · пора" if due((r['values'] + [''] * 6)[5]) else ""))
                   for r in items]
    emit(a, pieces)


# --- /contacts ---

def cmd_contacts(a):
    sys.path.insert(0, str(HERE.parent.parent / "job-contacts" / "scripts"))
    import contacts as C
    rows = C.read_tsv(C.CONTACTS, C.C_FIELDS)
    if not rows:
        emit(a, ["Контактов нет."]); return
    by = {}
    for r in rows:
        by.setdefault(r["company"], []).append(r)
    pieces = [f"<b>Контакты</b>: {len(rows)} в {len(by)} компаниях"]
    for comp, people in by.items():
        pieces.append(f"\n\n<b>{esc(comp)}</b>")
        for r in people:
            st = CONTACT_STATUS.get(r["status"], r["status"])
            when = f" {ddmm(r['last_contact_at'])}" if r["last_contact_at"] else ""
            src = f' · <a href="{esc(r["source_url"])}">источник</a>' if r.get("source_url", "").startswith("http") else ""
            pieces.append(f"\n{esc(r['name'])} <i>({esc(r['role'])})</i> — <code>{esc(r['value'])}</code>"
                          f" · <i>{esc(st)}{esc(when)}</i>{src}")
    emit(a, pieces)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name, f in (("queue", cmd_queue), ("tracker", cmd_tracker), ("contacts", cmd_contacts)):
        p = sp.add_parser(name)
        p.add_argument("--tg", action="store_true", help="HTML прямо в Telegram")
        p.set_defaults(f=f)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
