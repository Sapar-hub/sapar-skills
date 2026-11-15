#!/usr/bin/env python3
"""Митапы: RSS/Telegram -> фильтр -> пост в Telegram с кнопкой «Иду» -> пинг накануне и разбор после.

  events.py run [--dry-run]          забрать ленты, новые события в Telegram (Иду / Скип)
  events.py tick                     раз в 5 мин (timer): пинги «микроцель?» накануне и разбор через час после конца
  events.py go <id> | skip <id>      действия кнопок
  events.py show [--days N] [--all]  события из базы; --all — и не прошедшие фильтр
  events.py log                      походы: цель и разбор
  (в боте: /events = show --days 1, /eventlog = log)
  events.py parse "текст"            проверить распознавание даты
  events.py _ask <id> <date|eve|after>   (внутреннее) один вопрос в tg, ответ в базу

Ленты, фильтр и база — из job-digest (digest.py), со своим конфигом и своей базой:
  конфиг  $EVENT_DIGEST_CONFIG (~/.config/event-digest): feeds.txt (`<url> [link]`), keywords.txt
  база    $EVENT_DIGEST_STATE  (~/.local/state/event-digest/items.db): items (как в job-digest) + events
keywords.txt: обычная строка — тема (нужна хотя бы одна), `+regex` — место (нужно хотя бы одно),
`!regex` — исключение. Проверка по всему посту, не по строкам: анонс — это один пост на одно событие.
`link` после URL в feeds.txt: пост без кнопок, только ссылка (подборки на много событий).
"""
import datetime as dt, html, os, re, subprocess, sys
from pathlib import Path

os.environ["JOB_DIGEST_CONFIG"] = os.environ.get("EVENT_DIGEST_CONFIG", str(Path("~/.config/event-digest").expanduser()))
os.environ["JOB_DIGEST_STATE"] = os.environ.get("EVENT_DIGEST_STATE", str(Path("~/.local/state/event-digest").expanduser()))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "job-digest" / "scripts"))   # python -I не кладёт сюда сам
import digest as D  # noqa: E402

MONTHS = ["январ", "феврал", "март", "апрел", "ма[йя]", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"]
MONTH_RX = "|".join(f"({m})" for m in MONTHS)
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
DEFAULT_LEN = dt.timedelta(hours=3)               # конец не указан -> начало + 3 ч, end_is_guess=1
EVE_HOUR = 20                                     # «микроцель на завтра?» — накануне в 20:00
AFTER = dt.timedelta(hours=1)                     # разбор — через час после конца
TIMEOUT = {"date": 3 * 3600, "eve": 4 * 3600, "after": 6 * 3600}   # вопрос один на всех: молчание не держит очередь
AFTER_Q = "где было хуже всего → тело → слово → что сделал"


# --- даты ---

def parse_when(text, ref):
    """-> (start, end, start_is_guess, end_is_guess) или None, если дня нет.
    ref — дата публикации: от неё «завтра» и выбор года. start_is_guess — дня нет времени (поставлено 19:00)."""
    t = text.lower().replace("ё", "е")
    day = None
    for m in re.finditer(rf"\b(\d{{1,2}})(?:\s*(?:[-–—]|по)\s*\d{{1,2}})?\s+(?:{MONTH_RX})\w*(?:\s+(\d{{4}}))?", t):
        mon = next(i for i in range(12) if m.group(i + 2)) + 1
        day = (int(m.group(1)), mon, m.group(14) and int(m.group(14)), m.end())
        break
    if not day:
        m = re.search(r"(?<![\d.:])(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?(?![\d:])", t)
        if m and 1 <= int(m.group(2)) <= 12 and not re.search(r"(?:в|с|до|начало|старт)\s*$", t[:m.start()]):
            y = m.group(3) and int(m.group(3))
            day = (int(m.group(1)), int(m.group(2)), y and (y + 2000 if y < 100 else y), m.end())
    if not day:
        m = re.search(r"\b(сегодня|завтра|послезавтра)\b", t)
        if not m:
            return None
        d = ref + dt.timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[m.group(1)])
        day = (d.day, d.month, d.year, m.end())
    dd, mm, yy, pos = day
    if not yy:                                    # год не указан: ближайшая такая дата не раньше публикации
        yy = ref.year if (mm, dd) >= (ref.month, ref.day) or ref.month - mm < 6 else ref.year + 1
    try:
        date = dt.date(yy, mm, dd)
    except ValueError:
        return None
    # время: сначала рядом с датой, потом где угодно в тексте; «19:00–22:00» / «с 19:00 до 22:00»
    tm = r"(\d{1,2})[:.](\d{2})"
    rng = rf"{tm}(?:\s*(?:[-–—]|до)\s*{tm})?"
    m = re.search(rng, t[pos:pos + 80]) or re.search(rf"(?:\bв|\bс|начало|старт\w*)\s*{rng}", t) or re.search(
        rf"(?<![\d.]){tm}(?:\s*[-–—]\s*{tm})?(?![\d.])", t)
    ok = lambda h, mi: h is not None and int(h) < 24 and int(mi) < 60
    if m and ok(m.group(1), m.group(2)):
        start = dt.datetime.combine(date, dt.time(int(m.group(1)), int(m.group(2))))
        start_guess = 0
        if ok(m.group(3), m.group(4)):
            end = dt.datetime.combine(date, dt.time(int(m.group(3)), int(m.group(4))))
            end += dt.timedelta(days=1) if end <= start else dt.timedelta(0)
            return start, end, 0, 0
    else:
        start, start_guess = dt.datetime.combine(date, dt.time(19, 0)), 1
    return start, start + DEFAULT_LEN, start_guess, 1


def fmt(start, guess=0):
    s = dt.datetime.fromisoformat(start) if isinstance(start, str) else start
    return f"{WD[s.weekday()]} {s:%d.%m}" + ("" if guess else f" {s:%H:%M}")


# --- фильтр ---

def patterns():
    kw = D.read_list("keywords.txt")
    rx = lambda ks: [re.compile(k, re.I) for k in ks]
    return (rx(k for k in kw if k[0] not in "+!"), rx(k[1:] for k in kw if k.startswith("+")),
            rx(k[1:] for k in kw if k.startswith("!")))


def matches(text, pats):
    topic, place, exc = pats
    return (any(p.search(text) for p in topic) and (not place or any(p.search(text) for p in place))
            and not any(p.search(text) for p in exc))


def title_of(title, text):
    """Заголовок RSS (у Хабра — настоящий, у RSSHub/Telegram — первая строка поста), иначе первая строка."""
    line = D.to_text(title).strip() or next((l.strip() for l in text.split("\n") if l.strip()), "")
    return re.sub(r"^[^\w«\"(]+", "", line)[:120]


def feed_name(name):
    return "Хабр" if name.endswith("/ Хабр") else re.sub(r" - митапы и конференции.*$", "", name)


# --- база ---

def db():
    c = D.db()
    c.execute("""create table if not exists events(
        id integer primary key,            -- = items.rowid
        title text, link text, feed text,
        start text, "end" text, start_is_guess integer, end_is_guess integer,
        status text,                       -- new / going / need_date / skipped / cancelled / done / no_report
        goal text, report text,
        asked text,                        -- этапы, о которых уже спросили: date,eve,after
        ask_pid integer)""")
    return c


def get_event(c, eid):
    c.row_factory = __import__("sqlite3").Row
    r = c.execute("select * from events where id=?", (int(eid),)).fetchone()
    if not r:
        D.die(f"нет события {eid}")
    return dict(r)


def mark_asked(c, eid, stage):
    asked = (c.execute("select asked from events where id=?", (eid,)).fetchone()[0] or "").split(",")
    c.execute("update events set asked=? where id=?", (",".join(filter(None, set(asked) | {stage})), eid))


# --- run ---

def tg_send(text, buttons=()):
    tg = os.environ.get("EVENT_DIGEST_TG") or D.tg_bin()
    if not tg:
        print("WARN: нет tg (плагин tg-bridge)", file=sys.stderr)
        return False
    btns = [x for b in buttons for x in ("--button", b)]
    p = subprocess.run([tg, "send", "--html", text, *btns], capture_output=True, text=True)
    if p.returncode:
        print(f"WARN: tg send: {p.stderr.strip()}", file=sys.stderr)
    return not p.returncode


def event_text(e):
    x = html.escape
    when = fmt(e["start"], e["start_is_guess"]) if e["start"] else "дата?"
    if e["start"] and e["start_is_guess"]:
        when += ", время?"
    return f'<b>{x(e["title"])}</b>\n{when} · {x(e["feed"])} · <a href="{x(e["link"])}">пост</a>'


def cmd_run(a):
    pats, c = patterns(), db()
    now = dt.datetime.now()
    out = []
    for line in D.read_list("feeds.txt"):
        url, flags = line.split()[0], line.split()[1:]
        try:
            name, items = D.parse_feed(D.fetch(url))
        except Exception as e:
            print(f"WARN: {url}: {e}", file=sys.stderr)
            continue
        name = feed_name(name)
        for it in items:
            text = D.to_text(it["html"] or it["title"])
            if c.execute("select 1 from items where guid=?", (it["guid"],)).fetchone():
                continue
            try:
                pub = dt.datetime.fromisoformat(it["pub"])
                pub = pub.astimezone().replace(tzinfo=None) if pub.tzinfo else pub
            except ValueError:
                pub = now
            when = parse_when(text, pub.date())
            # прошло или без даты и старое -> только в архив
            ok = matches(f"{it['title']}\n{text}", pats) and (
                when[0] > now if when else (now - pub).days <= D.MAX_AGE_DAYS)
            if a.dry_run:
                if ok:
                    print(f"+ {name} | {fmt(when[0], when[2]) if when else 'дата?'} | {title_of(it['title'], text)}")
                continue
            cur = c.execute("insert into items(guid, feed, title, link, html, pub, matched, fetched_at, sent_at)"
                            " values(?,?,?,?,?,?,?,?,?)",
                            (it["guid"], name, D.to_text(it["title"]), it["link"], it["html"], it["pub"],
                             int(ok), now.isoformat(timespec="seconds"), None if ok else now.isoformat()))
            if ok:
                eid, link = cur.lastrowid, re.sub(r"\?utm_[^#]*$", "", it["link"])
                st, en, sg, eg = when or (None, None, 1, 1)
                c.execute('insert into events(id, title, link, feed, start, "end", start_is_guess, end_is_guess,'
                          ' status) values(?,?,?,?,?,?,?,?,?)',
                          (eid, title_of(it["title"], text), link, name,
                           st and st.isoformat(), en and en.isoformat(), sg, eg, "new"))
                out.append((eid, "link" in flags))
    c.commit()
    if a.dry_run:
        return
    print(f"новых событий: {len(out)}")
    for eid, link_only in out:
        e = get_event(c, eid)
        btn = [] if link_only else [f"Иду=event-go:{eid}", f"Скип=event-skip:{eid}"]
        if tg_send(event_text(e), btn):
            c.execute("update items set sent_at=? where rowid=?", (dt.datetime.now().isoformat(), eid))
            c.commit()


# --- кнопки ---

def spawn(eid, stage):
    """Вопрос в tg — отдельным процессом: кнопка (и tick) не ждут ответа часами."""
    p = subprocess.Popen([sys.executable, "-I", __file__, "_ask", str(eid), stage], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.pid


def cmd_go(a):
    c = db()
    e = get_event(c, a.id)
    if e["status"] in ("going", "need_date"):
        print("уже записано")
        return
    if e["start"] and not e["start_is_guess"]:
        c.execute("update events set status='going' where id=?", (e["id"],))
        c.commit()
        print(f"Иду: {fmt(e['start'])}" + (", конец не указан, считаю +3 ч" if e["end_is_guess"] else ""))
    else:
        c.execute("update events set status='need_date', asked=null where id=?", (e["id"],))
        c.commit()
        if asking(c):                                   # открытый вопрос уже есть: спросит tick, когда тот закроется
            print("Иду. Дату спрошу после текущего вопроса")
            return
        mark_asked(c, e["id"], "date")
        c.execute("update events set ask_pid=? where id=?", (spawn(e["id"], "date"), e["id"]))
        c.commit()
        print("Иду. Дату уточню следующим сообщением")


def cmd_skip(a):
    c = db()
    get_event(c, a.id)
    c.execute("update events set status='skipped' where id=?", (int(a.id),))
    c.commit()
    print("пропущено")


# --- вопросы ---

def ask(question, timeout):
    """-> ответ или None (таймаут/ошибка)."""
    tg = os.environ.get("EVENT_DIGEST_TG") or D.tg_bin()
    if not tg:
        return None
    p = subprocess.run([tg, "ask", question, "--text", "--timeout", str(int(max(60, timeout)))],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def cancelled(ans):
    return bool(re.match(r"\s*(не\s*(иду|пойду|пошел|был)|отмен|cancel|skip|скип)", ans or "", re.I))


def cmd_ask(a):
    c = db()
    e = get_event(c, a.id)
    eid, now = e["id"], dt.datetime.now()
    mark_asked(c, eid, a.stage)
    c.execute("update events set ask_pid=? where id=?", (os.getpid(), eid))
    c.commit()
    try:
        if a.stage == "date":
            q = (f"Когда «{e['title']}»? Например: 19.11 19:00 или 19.11 19:00-22:00. «не иду» — отменить.")
            for _ in range(2):
                ans = ask(q, TIMEOUT["date"])
                if ans is None:
                    c.execute("update events set status='new', asked=null where id=?", (eid,))   # «Иду» снова
                    break
                if cancelled(ans):
                    c.execute("update events set status='cancelled' where id=?", (eid,))
                    break
                w = parse_when(ans, now.date())
                if w and not w[2] and w[0] > now:
                    c.execute('update events set start=?, "end"=?, start_is_guess=0, end_is_guess=?, status=?'
                              ' where id=?', (w[0].isoformat(), w[1].isoformat(), w[3], "going", eid))
                    tg_send(f"Записал: {fmt(w[0])}, «{html.escape(e['title'])}»")
                    break
                q = "Не понял дату или она в прошлом. Нужно число, месяц и время: 19.11 19:00"
        elif a.stage == "eve":
            start = dt.datetime.fromisoformat(e["start"])
            ans = ask(f"{'Сегодня' if start.date() == now.date() else 'Завтра'} «{e['title']}», {fmt(start)}.\n"
                      "Микроцель — одна строка. «не иду» — отменить.", min(TIMEOUT["eve"], (start - now).total_seconds()))
            if cancelled(ans):
                c.execute("update events set status='cancelled' where id=?", (eid,))
            elif ans:
                c.execute("update events set goal=? where id=?", (ans, eid))
        elif a.stage == "after":
            goal = f"Цель была: {e['goal']}\n" if e["goal"] else ""
            ans = ask(f"Как прошло «{e['title']}»?\n{goal}{AFTER_Q}\n«не был» — если не пошёл.", TIMEOUT["after"])
            status = "cancelled" if cancelled(ans) else "done" if ans else "no_report"
            c.execute("update events set report=?, status=? where id=?", (ans, status, eid))
    finally:
        c.execute("update events set ask_pid=null where id=?", (eid,))
        c.commit()


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def asking(c):
    """Открыт ли уже вопрос: два текстовых вопроса сразу tg-bridge не различит без реплая."""
    return any(alive(pid) for (pid,) in c.execute("select ask_pid from events where ask_pid is not null"))


def due(e, now):
    """Какой вопрос пора задать по событию, или None."""
    asked = set((e["asked"] or "").split(","))
    if e["status"] == "need_date":
        return "reset" if "date" in asked else "date"  # спрашивали, а процесс умер (рестарт) -> снова «new»
    if e["status"] != "going" or not e["start"]:
        return None
    start, end = dt.datetime.fromisoformat(e["start"]), dt.datetime.fromisoformat(e["end"])
    eve = dt.datetime.combine(start.date() - dt.timedelta(days=1), dt.time(EVE_HOUR))
    if "eve" not in asked and eve <= now < start - dt.timedelta(minutes=30):
        return "eve"
    if "after" not in asked and now >= end + AFTER:
        return "after"
    return None


def cmd_tick(a):
    c = db()
    c.row_factory = __import__("sqlite3").Row
    now = dt.datetime.now()
    busy = asking(c)
    for e in map(dict, c.execute("select * from events where status in ('going', 'need_date') order by start")):
        stage = None if alive(e["ask_pid"]) else due(e, now)
        if stage and stage != "reset" and busy:
            continue                                    # по одному вопросу за раз; остальные — в следующие tick
        if stage == "reset":
            c.execute("update events set status='new', asked=null where id=?", (e["id"],))
            c.commit()
        elif stage:
            print(f"#{e['id']} {stage}: {e['title']}")
            if not a.dry_run:
                mark_asked(c, e["id"], stage)               # сразу: два tick подряд не спросят дважды
                c.execute("update events set ask_pid=? where id=?", (spawn(e["id"], stage), e["id"]))
                c.commit()
                busy = True


# --- просмотр ---

def cmd_show(a):
    c = db()
    since = (dt.datetime.now() - dt.timedelta(days=a.days)).isoformat()
    if a.all:
        for rowid, feed, title, matched in c.execute(
                "select rowid, feed, title, matched from items where fetched_at >= ? order by rowid", (since,)):
            print(f"{'+' if matched else '-'} #{rowid} {feed} | {title[:100]}")
        return
    c.row_factory = __import__("sqlite3").Row
    rows = c.execute("select * from events where coalesce(start, '9') >= ? order by start", (since[:10],)).fetchall()
    if not rows:
        print("Ближайших событий нет. Анонсы приходят в 10:17.")
    for e in rows:
        print(f"#{e['id']} {e['status']:9} {fmt(e['start'], e['start_is_guess']) if e['start'] else 'дата?':11}"
              f" {e['title'][:70]} <{e['link']}>")


def cmd_log(a):
    c = db()
    c.row_factory = __import__("sqlite3").Row
    rows = c.execute("select * from events where status in ('done', 'no_report', 'going') and goal is not null"
                     " or report is not null order by start").fetchall()
    if not rows:
        print("Разборов пока нет: появятся после первого похода.")
    for e in rows:
        print(f"== {fmt(e['start'])} {e['title']} [{e['status']}]\n  цель: {e['goal'] or '-'}\n"
              f"  разбор: {e['report'] or '-'}")


def cmd_parse(a):
    w = parse_when(a.text, dt.date.today())
    print("нет дня" if not w else f"{w[0]:%Y-%m-%d %H:%M} – {w[1]:%H:%M}  start_guess={w[2]} end_guess={w[3]}")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("run").add_argument("--dry-run", action="store_true")
    sp.add_parser("tick").add_argument("--dry-run", action="store_true")
    sp.add_parser("go").add_argument("id")
    sp.add_parser("skip").add_argument("id")
    s = sp.add_parser("show")
    s.add_argument("--days", type=int, default=1)
    s.add_argument("--all", action="store_true")
    sp.add_parser("log")
    sp.add_parser("parse").add_argument("text")
    q = sp.add_parser("_ask")
    q.add_argument("id")
    q.add_argument("stage", choices=["date", "eve", "after"])
    a = ap.parse_args()
    {"run": cmd_run, "tick": cmd_tick, "go": cmd_go, "skip": cmd_skip, "show": cmd_show, "log": cmd_log,
     "parse": cmd_parse, "_ask": cmd_ask}[a.cmd](a)


if __name__ == "__main__":
    main()
