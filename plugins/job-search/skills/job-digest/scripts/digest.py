#!/usr/bin/env python3
"""RSS (RSSHub/Telegram и любые ленты) -> фильтр по ключевым словам -> дайджест в папку Mail.ru.

  digest.py run [--dry-run] [--force] [--tg]   # забрать ленты, дайджест в почту (+ каждый пост в Telegram)
  digest.py show [--days N] [--all]     # вывести найденное из локальной базы
  digest.py feeds                       # проверить, что ленты отвечают
  digest.py tg <id>...                  # (пере)прислать посты в Telegram с кнопками
  digest.py item <id>                   # весь пост и вакансии в нём (для job-apply)
  digest.py queue <id.n> | skip <id> | bl <id.n>   # действия кнопок: в очередь откликов / скип / на разбор в blacklist

Telegram: команда `tg` из плагина tg-bridge. Кнопки — tg actions digest-queue/-skip/-bl (их ставит install.sh).
Очередь откликов: $JOB_APPLY_HOME/queue.tsv (по умолчанию ~/.local/share/job-apply) — её разбирает job-apply worker.

Конфиг:  $JOB_DIGEST_CONFIG (по умолчанию ~/.config/job-digest): feeds.txt, keywords.txt
Состояние: $JOB_DIGEST_STATE (по умолчанию ~/.local/state/job-digest/items.db)
Почта:   MAILRU_USER, MAILRU_APPPASS (пароль для внешних приложений), JOB_DIGEST_FOLDER (Вакансии)
"""
import argparse, base64, datetime as dt, email.utils, html, imaplib, os, re, shutil, sqlite3, subprocess, sys, time
import urllib.request, xml.etree.ElementTree as ET
from email.message import EmailMessage
from pathlib import Path

CONF = Path(os.environ.get("JOB_DIGEST_CONFIG", "~/.config/job-digest")).expanduser()
STATE = Path(os.environ.get("JOB_DIGEST_STATE", "~/.local/state/job-digest")).expanduser()
FOLDER = os.environ.get("JOB_DIGEST_FOLDER", "Вакансии")
IMAP_HOST = "imap.mail.ru"
MAX_AGE_DAYS = int(os.environ.get("JOB_DIGEST_MAX_AGE_DAYS", 14))
ATOM = "{http://www.w3.org/2005/Atom}"
APPLY_HOME = Path(os.environ.get("JOB_APPLY_HOME", "~/.local/share/job-apply")).expanduser()
QUEUE_FIELDS = ["date", "key", "status", "feed", "title", "href", "details", "post", "note"]  # = job-apply/scripts/queue.py


def die(msg):
    print(f"ERR: {msg}", file=sys.stderr)
    sys.exit(1)


def read_list(name):
    p = CONF / name
    if not p.exists():
        die(f"нет {p} — запусти install.sh")
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


# --- ленты ---

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "job-digest/1.0"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return ET.fromstring(r.read())


def parse_feed(root):
    """-> (название ленты, [dict(guid, title, link, html, pub)]) для RSS 2.0 и Atom."""
    items = []
    if root.tag == f"{ATOM}feed":
        name = root.findtext(f"{ATOM}title") or ""
        for e in root.findall(f"{ATOM}entry"):
            link = e.find(f"{ATOM}link")
            items.append(dict(
                guid=e.findtext(f"{ATOM}id") or "", title=e.findtext(f"{ATOM}title") or "",
                link=link.get("href", "") if link is not None else "",
                html=e.findtext(f"{ATOM}content") or e.findtext(f"{ATOM}summary") or "",
                pub=e.findtext(f"{ATOM}updated") or ""))
    else:
        ch = root.find("channel")
        name = ch.findtext("title") or ""
        for i in ch.findall("item"):
            pub = i.findtext("pubDate") or ""
            try:
                pub = email.utils.parsedate_to_datetime(pub).isoformat()
            except (TypeError, ValueError):
                pass
            items.append(dict(
                guid=i.findtext("guid") or i.findtext("link") or "", title=i.findtext("title") or "",
                link=i.findtext("link") or "", html=i.findtext("description") or "", pub=pub))
    name = re.sub(r"\s*-\s*Telegram Channel$", "", html.unescape(name))
    return name, [it for it in items if it["guid"]]


# --- текст и фильтр ---

def clean_html(h):
    h = re.sub(r'\s+on\w+="[^"]*"', "", h)                    # onclick из Telegram
    h = re.sub(r"</?tg-emoji[^>]*>", "", h)
    return h


def to_text(h):
    t = re.sub(r"<br\s*/?>|</p>|</div>", "\n", h, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"[ \t ]+", " ", t).strip()


def load_patterns():
    """keywords.txt: обычная строка — нужное слово, `+regex` — слово уровня (стажёр, джун),
    `!regex` — исключение (по строке и деталям). -> (нужные, исключения, уровень, направление)."""
    kw = read_list("keywords.txt")
    rx = lambda ks: [re.compile(k, re.I) for k in ks]
    level = [k[1:] for k in kw if k.startswith("+")]
    topic = [k for k in kw if k[0] not in "+!"]
    return rx(level + topic), rx(k[1:] for k in kw if k.startswith("!")), rx(level), rx(topic)


def read_feeds():
    """feeds.txt: `<url> [strict]` -> [(url, strict)]."""
    return [(l.split()[0], "strict" in l.split()[1:]) for l in read_list("feeds.txt")]


def strict_hits(it, patterns):
    """Строгий порог для шумных лент: в посте есть слово уровня, а в строке вакансии — направление.
    Пост-вакансия без списка проходит по заголовку."""
    inc, exc, level, topic = patterns
    text = to_text(it["html"] or it["title"])
    if not any(p.search(text) for p in level):
        return []
    # пост-вакансия пишет город как «Офис (Ташкент)»: нужна Москва или удалёнка
    places = re.findall(r"(?:офис|гибрид|on-?site|office|hybrid)\W{0,3}\(([^)]{2,60})\)", text, re.I)
    if places and not any(re.search(r"москв|moscow|удал|remote", p, re.I) for p in places):
        return []
    out = [h for h in hits(it["html"] or it["title"], patterns)
           if any(p.search(f"{h[0]} {h[2]}") for p in topic)]
    lines = [l.strip() for l in text.split("\n") if l.strip() and not re.match(r"(🔁\s*)?Forwarded From", l.strip())]
    title = (lines or [""])[0][:140]
    if not out and any(p.search(title) for p in topic) and not any(p.search(title) for p in exc):
        out = [(title, it["link"], "")]
    return out


def hits(h, patterns):
    """Строки поста, где сработало ключевое слово: [(строка, ссылка, детали)]."""
    inc, exc = patterns[:2]
    lines = re.split(r"<br\s*/?>", clean_html(h), flags=re.I)
    out = []
    for n, line in enumerate(lines):
        text = to_text(line)
        # вакансия в подборке — короткая строка со ссылкой или со строкой условий под ней;
        # абзацы, опросы и сами строки условий («без опыта | Москва») пропускаем
        if not text or len(text) > 140 or "|" in text or not any(p.search(text) for p in inc):
            continue
        nxt = to_text(lines[n + 1]) if n + 1 < len(lines) else ""
        det = nxt if "|" in nxt else ""
        href = re.search(r'href="([^"]+)"', line)
        href = html.unescape(href.group(1)) if href else ""
        if not (href or det) or any(p.search(f"{text} {det} {href}") for p in exc):
            continue
        out.append((text, href, det))
    return out


# --- база ---

def db():
    STATE.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(STATE / "items.db")
    c.execute("""create table if not exists items(
        guid text primary key, feed text, title text, link text, html text, pub text,
        matched integer, fetched_at text, sent_at text)""")
    if "state" not in [r[1] for r in c.execute("pragma table_info(items)")]:
        c.execute("alter table items add column state text")    # queued / skipped / bl-review (кнопки в Telegram)
    return c


# --- почта ---

def imap_utf7(s):
    """Имя папки в modified UTF-7 (RFC 3501), иначе кириллица не пройдёт."""
    out, buf = [], ""
    def flush():
        if buf:
            b = base64.b64encode(buf.encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{b}-")
    for ch in s:
        if 0x20 <= ord(ch) <= 0x7e:
            flush(); buf = ""
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    flush()
    return "".join(out)


def post_hits(r, patterns):
    return strict_hits(r, patterns) if r["matched"] == 2 else hits(r["html"], patterns)


def found_posts(rows, patterns):
    """Подходящие посты с их вакансиями; одна вакансия в нескольких каналах (репосты) — один раз."""
    seen, found = set(), []
    for r in rows:
        if not r["matched"]:
            continue
        hs = [h for h in post_hits(r, patterns) if not h[1] or h[1] == r["link"] or h[1] not in seen]
        seen.update(h[1] for h in hs)
        if hs:
            found.append((r, hs))
    return found


def build_message(rows, patterns, user):
    found = found_posts(rows, patterns)
    rest = [r for r in rows if not r["matched"]]
    feeds = sorted({r["feed"] for r, _ in found})
    subj = f"Вакансии: {len(found)} подходящих ({', '.join(feeds)}) — {dt.date.today():%d.%m}"

    txt, body = [], [f"<h2>{html.escape(subj)}</h2>"]
    for r, hs in found:
        txt.append(f"== {r['feed']} | {r['pub'][:10]} | {r['link']}")
        body.append(f'<hr><p><b>{html.escape(r["feed"])}</b> · {r["pub"][:10]} · '
                    f'<a href="{html.escape(r["link"])}">пост</a></p><ul>')
        for text, href, det in hs:
            txt.append(f"  * {text}{' — ' + det if det else ''}{' <' + href + '>' if href else ''}")
            t = html.escape(text)
            body.append(f'<li><b>{f"<a href={chr(34)}{html.escape(href)}{chr(34)}>{t}</a>" if href else t}</b>'
                        f'{"<br>" + html.escape(det) if det else ""}</li>')
        body.append("</ul><details><summary>весь пост</summary>"
                    f"<blockquote>{clean_html(r['html'])}</blockquote></details>")
        txt.append(to_text(r["html"]) + "\n")
    if rest:
        body.append("<hr><h3>Прочее (не прошло фильтр)</h3><ul>")
        txt.append("== Прочее")
        for r in rest:
            body.append(f'<li><a href="{html.escape(r["link"])}">{html.escape(r["title"][:120])}</a></li>')
            txt.append(f"  - {r['title'][:120]} <{r['link']}>")
        body.append("</ul>")

    msg = EmailMessage()
    msg["From"] = msg["To"] = user
    msg["Subject"] = subj
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain="job-digest.local")
    msg.set_content("\n".join(txt))
    msg.add_alternative("<html><body>" + "\n".join(body) + "</body></html>", subtype="html")
    return msg


def deliver(msg):
    user, pw = os.environ.get("MAILRU_USER"), os.environ.get("MAILRU_APPPASS")
    if not (user and pw):
        die("нужны MAILRU_USER и MAILRU_APPPASS")
    box = imap_utf7(FOLDER)
    with imaplib.IMAP4_SSL(IMAP_HOST) as m:
        m.login(user, pw)
        m.create(f'"{box}"')                                     # уже есть -> NO, это нормально
        typ, data = m.append(f'"{box}"', "", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if typ != "OK":
            die(f"IMAP APPEND: {data}")


# --- команды ---

def cmd_run(a):
    patterns = load_patterns()
    c, now, new = db(), dt.datetime.now().isoformat(timespec="seconds"), 0
    cutoff = (dt.date.today() - dt.timedelta(days=MAX_AGE_DAYS)).isoformat()
    for url, strict in read_feeds():
        try:
            name, items = parse_feed(fetch(url))
        except Exception as e:                                   # одна сломанная лента не валит остальные
            print(f"WARN: {url}: {e}", file=sys.stderr)
            continue
        for it in items:
            if strict:                                           # matched=2; мимо фильтра — не в «Прочее», сразу в архив
                m = 2 if strict_hits(it, patterns) else 0
            else:
                m = 1 if hits(it["html"] or it["title"], patterns) else 0
            # архив новой ленты старше MAX_AGE_DAYS не шлём, только запоминаем
            skip = (strict and not m) or bool(it["pub"]) and it["pub"][:10] < cutoff
            cur = c.execute("insert or ignore into items(guid, feed, title, link, html, pub, matched, fetched_at, sent_at)"
                            " values(?,?,?,?,?,?,?,?,?)",                # явные колонки: state добавлен миграцией
                            (it["guid"], name, to_text(it["title"]), it["link"], it["html"], it["pub"], m, now,
                             now if skip else None))
            new += cur.rowcount
    c.commit()

    rows = [dict(zip(COLS, r)) for r in c.execute(
        f"select {', '.join(COLS)} from items where sent_at is null order by pub")]
    n_match = sum(1 for r in rows if r["matched"])
    print(f"новых: {new}, ждут отправки: {len(rows)}, подходящих: {n_match}")
    if not n_match and not (a.force and rows):
        return
    msg = build_message(rows, patterns, os.environ.get("MAILRU_USER", "me@localhost"))
    if a.dry_run:
        print(msg.get_body(("plain",)).get_content())
        if a.tg:
            for r, hs in found_posts(rows, patterns):
                print("--- tg ---\n" + tg_text(r, hs))
        return
    deliver(msg)
    c.executemany("update items set sent_at=? where guid=?", [(now, r["guid"]) for r in rows])
    c.commit()
    print(f"OK: «{msg['Subject']}» -> папка {FOLDER}")
    if a.tg:
        push_tg(found_posts(rows, patterns))


# --- Telegram (tg-bridge) ---

COLS = ("rowid", "guid", "feed", "title", "link", "html", "pub", "matched", "state")


def tg_bin():
    return shutil.which("tg") or (str(Path("~/.local/bin/tg").expanduser())
                                  if Path("~/.local/bin/tg").expanduser().exists() else None)


def tg_text(r, hs):
    e = html.escape
    out = [f'<b>{e(r["feed"])}</b>, {r["pub"][8:10]}.{r["pub"][5:7]} · <a href="{e(r["link"])}">пост</a>']
    for n, (text, href, det) in enumerate(hs[:8], 1):
        text = re.sub(r"^[^\w«\"(]+", "", text)              # маркеры и эмодзи каналов в начале: «💙 - стажёр…» → «стажёр…»
        t = f'<a href="{e(href)}">{e(text)}</a>' if href else e(text)
        out.append(f"{n}. {t}" + (f"\n    <i>{e(det)}</i>" if det else ""))
    if len(hs) > 8:
        out.append(f"и ещё {len(hs) - 8} в посте")
    return "\n".join(out)


def push_tg(found):
    tg = tg_bin()
    if not tg:
        print("WARN: нет tg (плагин tg-bridge) — в Telegram не отправлено", file=sys.stderr)
        return
    for r, hs in found:
        btns = []
        for n in range(1, min(len(hs), 8) + 1):
            btns += ["--button", f"{n}=digest-queue:{r['rowid']}.{n}"]
        btns += ["--button", f"Скип=digest-skip:{r['rowid']}"]
        if len(hs) == 1:
            btns += ["--button", f"В blacklist=digest-bl:{r['rowid']}.1"]
        p = subprocess.run([tg, "send", "--html", tg_text(r, hs), *btns], capture_output=True, text=True)
        if p.returncode:
            print(f"WARN: tg send: {p.stderr.strip()}", file=sys.stderr)
        time.sleep(1.1)                                        # ~1 сообщение/с в один чат


def get_row(c, rowid):
    row = c.execute(f"select {', '.join(COLS)} from items where rowid=?", (int(rowid),)).fetchone()
    if not row:
        die(f"нет поста {rowid}")
    return dict(zip(COLS, row))


def parse_key(key):
    rowid, _, n = key.partition(".")
    return int(rowid), int(n or 1)


def queue_add(c, key, status):
    rowid, n = parse_key(key)
    r = get_row(c, rowid)
    hs = post_hits(r, load_patterns()) or [(r["title"], r["link"], "")]
    if not 1 <= n <= len(hs):
        die(f"в посте {rowid} нет вакансии {n}")
    text, href, det = hs[n - 1]
    text = re.sub(r"^[^\w«\"(]+", "", text)
    f = APPLY_HOME / "queue.tsv"
    f.parent.mkdir(parents=True, exist_ok=True)
    rows = [l.split("\t") for l in f.read_text(encoding="utf-8").splitlines()[1:]] if f.exists() else []
    if any(x[1] == f"{rowid}.{n}" for x in rows):
        print("уже в очереди")
        return False
    new = not f.exists()
    with f.open("a", encoding="utf-8") as out:
        if new:
            out.write("\t".join(QUEUE_FIELDS) + "\n")
        clean = lambda s: re.sub(r"[\t\n]+", " ", s or "")
        out.write("\t".join(map(clean, [dt.date.today().isoformat(), f"{rowid}.{n}", status, r["feed"],
                                         text, href, det, r["link"], ""])) + "\n")
    c.execute("update items set state=? where rowid=?", ("queued" if status == "new" else status, rowid))
    c.commit()
    print({"new": "в очереди на отклик", "bl-review": "на разбор в blacklist"}[status])
    return True


def cmd_queue(a):
    if queue_add(db(), a.key, "new") and shutil.which("systemctl"):
        # worker job-apply (если поставлен) разбирает очередь; --no-block: кнопка не ждёт отклика
        subprocess.run(["systemctl", "--user", "start", "--no-block", "job-apply-worker.service"],
                       capture_output=True)


def cmd_bl(a):
    queue_add(db(), a.key, "bl-review")


def cmd_skip(a):
    c = db()
    get_row(c, a.id)
    c.execute("update items set state='skipped' where rowid=?", (int(a.id),))
    c.commit()
    print("пропущено")


def cmd_item(a):
    r = get_row(db(), a.id)
    print(f"== {r['feed']} | {r['pub'][:10]} | {r['link']} | state={r['state'] or '-'}")
    for n, (text, href, det) in enumerate(post_hits(r, load_patterns()), 1):
        print(f"  {n}. {text}{' — ' + det if det else ''}{' <' + href + '>' if href else ''}")
    print("\n" + to_text(r["html"]))


def cmd_tg(a):
    c, patterns = db(), load_patterns()
    found = [(r, post_hits(r, patterns) or [(r["title"], r["link"], "")]) for r in (get_row(c, i) for i in a.ids)]
    push_tg(found)


def cmd_show(a):
    patterns = load_patterns()
    since = (dt.datetime.now() - dt.timedelta(days=a.days)).isoformat()
    q = "select rowid, feed, pub, link, html, title, matched, state from items where (fetched_at >= ? or pub >= ?)"
    q += "" if a.all else " and matched > 0"
    for rowid, feed, pub, link, h, title, m, state in db().execute(q + " order by pub desc", (since, since)):
        print(f"== #{rowid} | {feed} | {pub[:10]} | {link}{' | ' + state if state else ''}")
        hs = strict_hits(dict(html=h, title=title, link=link), patterns) if m == 2 else hits(h, patterns)
        for text, href, det in hs or [(title, "", "")]:
            print(f"  * {text}{' — ' + det if det else ''}{' <' + href + '>' if href else ''}")


def cmd_feeds(a):
    for url, strict in read_feeds():
        try:
            name, items = parse_feed(fetch(url))
            print(f"OK  {len(items):3d}  {name}  {url}{'  [strict]' if strict else ''}")
        except Exception as e:
            print(f"ERR {url}: {e}")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="напечатать дайджест, не отправлять и не помечать")
    r.add_argument("--force", action="store_true", help="отправить, даже если подходящих нет")
    r.add_argument("--tg", action="store_true", help="ещё и каждый подходящий пост в Telegram с кнопками")
    s = sp.add_parser("show")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--all", action="store_true", help="включая не прошедшие фильтр")
    sp.add_parser("feeds")
    sp.add_parser("tg").add_argument("ids", nargs="+")
    sp.add_parser("item").add_argument("id")
    sp.add_parser("queue").add_argument("key")
    sp.add_parser("bl").add_argument("key")
    sp.add_parser("skip").add_argument("id")
    a = ap.parse_args()
    {"run": cmd_run, "show": cmd_show, "feeds": cmd_feeds, "tg": cmd_tg, "item": cmd_item,
     "queue": cmd_queue, "bl": cmd_bl, "skip": cmd_skip}[a.cmd](a)


if __name__ == "__main__":
    main()
