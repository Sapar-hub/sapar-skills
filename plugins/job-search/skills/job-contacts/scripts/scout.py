#!/usr/bin/env python3
"""scout: посты о найме в целевых компаниях → люди с рабочим контактом → связь с откликом в трекере → Telegram.

  scout.py run [--dry-run] [--no-tg] [--days N]   # собрать источники + ленты дайджеста, найти, прислать новое
  scout.py show [--days N] [--all]                # что нашлось (из базы)
  scout.py sources                                # живы ли источники
  scout.py add <hit>.<k>                          # кнопка: k-й контакт находки → contacts.tsv (источник = пост)
  scout.py skip <hit>                             # кнопка: находка не интересна
  scout.py ctx <@handle|email|url>                # где и когда встречался человек (контекст для письма)

Что ищем. Находка = пост, где есть (1) целевая компания (companies.txt или company= у источника),
(2) сигнал найма (hire:) и направление (topic:), без исключений (!) в заголовке. Контакты — только
опубликованные в самом посте (email, @handle, t.me/…), роль — по словам рядом (role-*:). Для статей Habr
находка = автор статьи по данным/ML в компании, где есть активный отклик (контакт = публичный профиль Habr).
Пост с активным откликом в трекере (кэш листа job-apply/status.py + applied.tsv) идёт первым и помечается.

Граница источников та же, что у job-contacts: публичное и рабочее, с URL. Личные аккаунты не автоматизируем.

Конфиг: $JOB_CONTACTS_CONFIG/scout (по умолчанию ~/.config/job-contacts/scout): sources.txt, companies.txt,
        signals.txt (дефолты — ../defaults/scout, копируются при первом запуске).
Состояние: ~/.local/state/job-contacts/scout.db. Ленты дайджеста: ~/.local/state/job-digest/items.db (только чтение).
"""
import argparse, datetime as dt, html, importlib.util, json, os, re, shutil, sqlite3, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF = Path(os.environ.get("JOB_CONTACTS_CONFIG", "~/.config/job-contacts")).expanduser() / "scout"
DEFAULTS = HERE.parent / "defaults" / "scout"
STATE = Path(os.environ.get("JOB_CONTACTS_STATE", "~/.local/state/job-contacts")).expanduser()
DIGEST_DB = Path(os.environ.get("JOB_DIGEST_STATE", "~/.local/state/job-digest")).expanduser() / "items.db"
ACTIVE = re.compile(r"ожидани|в процессе|действие|оффер", re.I)
MAX_BTN = 6
OTHER = "не из списка"                            # компания вне companies.txt (агрегатор, стажёр/джун, есть контакт)
MAX_AUTHORS = int(os.environ.get("SCOUT_AUTHORS", 0))   # авторов Habr в TG за прогон: слабый сигнал, по умолчанию не шлём (в базе и ctx остаются)
CHANNELISH = re.compile(r"job|vacanc|вакан|career|digest|chat|news|channel|_bot$|hiring", re.I)   # хэндл канала, не человека


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


C = load("contacts", HERE / "contacts.py")                                       # EMAIL, TG, read_tsv, …
D = load("digest", HERE.parents[1] / "job-digest" / "scripts" / "digest.py")    # fetch, parse_feed, to_text, tg_bin


# --- конфиг ---

def lines(name):
    p = CONF / name
    if not p.exists():
        CONF.mkdir(parents=True, exist_ok=True)
        shutil.copy(DEFAULTS / name, p)
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.lstrip().startswith("#")]


def rx(parts):
    return re.compile("|".join(f"(?:{p.strip()})" for p in parts if p.strip()), re.I) if parts else None


def load_companies():
    out = []
    for l in lines("companies.txt"):
        name, _, pats = l.partition(":")
        out.append((name.strip(), rx(pats.split(" | "))))
    return out


def load_signals():
    s = {"hire": [], "topic": [], "author-topic": [], "level": [], "!": [], "role-recruiter": [], "role-lead": []}
    for l in lines("signals.txt"):
        if l.startswith("!"):
            s["!"] += l[1:].split(" | ")
        else:
            k, _, v = l.partition(":")
            s.setdefault(k.strip(), []).extend(v.split(" | "))
    return {k: rx(v) for k, v in s.items()}


def load_sources():
    out = []
    for l in lines("sources.txt"):
        p = l.split()
        opts = dict(x.split("=", 1) for x in p[2:] if "=" in x)
        out.append({"url": p[0], "kind": p[1] if len(p) > 1 else "tg", "company": opts.get("company")})
    return out


# --- трекер: активные отклики по компаниям ---

def active_apps(companies):
    """-> {канон: [«позиция (дата, статус)»]} из кэша листа (job-apply status.py) + applied.tsv."""
    home = C.HOME
    rows, warn = [], None
    cache = home / "sheet-cache.json"
    if cache.exists():
        j = json.loads(cache.read_text())
        age = (time.time() - j.get("ts", 0)) / 86400
        if age > 3:
            warn = f"кэш трекера старый ({age:.0f} дн.) — обнови: job-apply/scripts/status.py"
        for r in j.get("rows", []):
            v = r["values"] + [""] * 6
            if ACTIVE.search(v[4]):
                rows.append((v[0], v[1], v[2], v[4]))
    else:
        warn = "нет кэша трекера (job-apply/scripts/status.py) — беру только applied.tsv"
    seen = {(C.norm(a), C.norm(b)) for a, b, *_ in rows}
    for a in C.read_tsv(C.APPLIED, ["date", "company", "title", "url", "resume", "status"]):
        if (C.norm(a["company"]), C.norm(a["title"])) not in seen and not C.CLOSED.search(a["status"]) and not cache.exists():
            rows.append((a["company"], a["title"], a["date"], a["status"]))
    out = {}
    for comp, title, date, status in rows:
        for name, r in companies:
            if r.search(comp):
                out.setdefault(name, []).append(f"{title} ({date[8:10]}.{date[5:7]}, {status})")
                break
    return out, warn


# --- разбор поста ---

def company_of(text, companies, forced=None):
    if forced:
        return forced
    for name, r in companies:
        if r.search(text):
            return name
    return None


def role_near(text, start, end, sig):
    w = text[max(0, start - 80):end + 80]
    if sig["role-recruiter"] and sig["role-recruiter"].search(w):
        return "recruiter"
    if sig["role-lead"] and sig["role-lead"].search(w):
        return "lead"
    return "other"


def contacts_in(text, sig, own=()):
    """Контакты, опубликованные в посте: [(channel, value, role, context)]. own — хэндлы самого источника."""
    out, seen = [], set(h.lower() for h in own)
    for m in C.EMAIL.finditer(text):
        v = m.group(0).lower().rstrip(".")
        if v in seen or v.endswith((".png", ".jpg", ".svg", ".webp")):
            continue
        seen.add(v)
        out.append(("email", v, role_near(text, m.start(), m.end(), sig), C.ctx(text, m, 60)))
    for m in C.TG.finditer(text):
        h = m.group(1) or m.group(2)
        if h.lower() in C.TG_SKIP or h.lower() in seen or h.lower().endswith("bot"):
            continue
        seen.add(h.lower())
        out.append(("tg", "@" + h, role_near(text, m.start(), m.end(), sig), C.ctx(text, m, 60)))
    return out


def known_values():
    return {C.norm(r["value"]) for r in C.read_tsv(C.CONTACTS, C.C_FIELDS)}


def analyze(it, src, companies, sig, known):
    """it: dict(guid,title,link,html,pub,feed,author) → находка dict или None."""
    text = D.to_text(it.get("html") or "")
    full = f"{it.get('title') or ''}\n{text}"
    head = re.sub(r"^[^\w«\"(#]+", "", html.unescape(it.get("title") or text.split("\n", 1)[0]))[:200]
    kind = src["kind"]
    comp = company_of(full + "\n" + (it.get("author") or ""), companies, src.get("company"))
    if not comp:
        return None
    if kind == "habr":
        if not (sig.get("author-topic") or sig["topic"]).search(full):
            return None
        author = (it.get("author") or "").split(" (")[0].strip()
        if not author or any(r.search(author) for _, r in companies) or re.search(r"(?i)^(team|blog|press|pr)_?|_(team|ai|tech)$", author):
            return None                                    # корпоративный аккаунт блога, а не человек
        return dict(kind="author", company=comp, head=head, text=text[:600], link=it["link"].split("?")[0],
                    contacts=[("other", f"https://habr.com/ru/users/{author}/", "engineer", f"автор статьи «{head[:80]}»")],
                    author=author)
    if sig["!"] and sig["!"].search(head):
        return None
    if kind != "career" and not sig["hire"].search(full):
        return None
    if not sig["topic"].search(head if kind == "career" else full):   # career: описание шаблонное, смотрим заголовок
        return None
    own = [m.group(1) for m in re.finditer(r"t\.me/(?:s/)?([\w]+)", src["url"] + " " + it.get("link", ""))]
    own += [src["url"].rstrip("/").rsplit("/", 1)[-1]]
    allc = [c for c in contacts_in(full, sig, own)
            if not (c[0] == "tg" and c[2] != "recruiter" and CHANNELISH.search(c[1]))]
    cs = [c for c in allc if C.norm(c[1]) not in known]
    seen_known = [c for c in allc if C.norm(c[1]) in known]      # уже в contacts.tsv: кнопки не нужны, но встречу пишем
    if not src.get("company") and kind != "career":
        # агрегатор: ценность только в человеке; компания — та, что рядом с контактом (в подборках их десятки)
        if not allc:
            return None
        i = full.find(allc[0][1].lstrip("@"))
        comp = company_of(full[max(0, i - 400):i + 100], companies) if i >= 0 else None
        if not comp:                                       # контакт далеко от имени: годится, если компания в посте одна
            hit = [n for n, r in companies if r.search(full)]
            comp = hit[0] if len(hit) == 1 else None
        if not comp:                                       # не из списка: берём, только если это стажёр/джун
            if not (sig.get("level") and sig["level"].search(full)):
                return None
            comp = OTHER
    return dict(kind="hire", company=comp, head=head, text=text[:600], link=it["link"], contacts=cs,
                known=seen_known, author=it.get("author") or it.get("feed") or "")


# --- база ---

def db():
    STATE.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(STATE / "scout.db")
    c.row_factory = sqlite3.Row
    c.execute("""create table if not exists seen(guid text primary key, at text)""")
    c.execute("""create table if not exists hits(id integer primary key, guid text unique, source text, kind text,
        company text, head text, text text, link text, pub text, author text, contacts text, apps text,
        found_at text, sent_at text, state text)""")
    # каждая встреча человека в посте — и новая, и повторная: контекст для «нашёл вас там-то»
    c.execute("""create table if not exists sightings(value text, channel text, role text, context text,
        company text, head text, link text, pub text, source text, at text, primary key (value, link))""")
    return c


def sight(c, h, it, src, now):
    for ch, v, role, context in h["contacts"] + h.get("known", []):
        c.execute("insert or ignore into sightings values (?,?,?,?,?,?,?,?,?,?)",
                  (C.norm(v), ch, role, context, h["company"], h["head"], h["link"], it.get("pub") or now, src["url"], now))


def items_from_sources(srcs, days):
    """-> [(src, item)]; ошибки источников — в stderr, прогон не роняют."""
    out = []
    for s in srcs:
        try:
            root = D.fetch(s["url"])
        except Exception as e:
            print(f"WARN: {s['url']}: {e}", file=sys.stderr)
            continue
        name, items = D.parse_feed(root)
        for it in items:
            it["feed"] = name
            it.setdefault("author", "")
        auth = author_map(root)
        for it in items:
            it["author"] = auth.get(it["guid"], it["author"])
        out += [(s, it) for it in items if fresh(it.get("pub"), days)]
    return out


def author_map(root):
    """RSS <author>/<dc:creator> по guid (parse_feed дайджеста их не берёт)."""
    m = {}
    ch = root.find("channel")
    for i in (ch.findall("item") if ch is not None else []):
        a = i.findtext("author") or i.findtext("{http://purl.org/dc/elements/1.1/}creator") or ""
        m[i.findtext("guid") or i.findtext("link") or ""] = a.strip()
    return m


def fresh(pub, days):
    try:
        t = dt.datetime.fromisoformat(pub) if "T" in (pub or "") else dt.datetime.strptime(pub[:25], "%a, %d %b %Y %H:%M:%S")
    except (TypeError, ValueError):
        return True
    t = t.replace(tzinfo=None)
    return (dt.datetime.now() - t).days <= days


def items_from_digest(days):
    if not DIGEST_DB.exists():
        return []
    c = sqlite3.connect(f"file:{DIGEST_DB}?mode=ro", uri=True)
    since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
    rows = c.execute("select guid, feed, title, link, html, pub from items where fetched_at >= ?", (since,)).fetchall()
    return [({"url": "digest:" + r[1], "kind": "tg", "company": None},
             dict(guid=r[0], feed=r[1], title=r[2], link=r[3], html=r[4], pub=r[5], author=r[1])) for r in rows]


# --- Telegram ---

def tg_msg(h):
    e = html.escape
    cs, apps = json.loads(h["contacts"]), json.loads(h["apps"] or "[]")
    tag = "найм" if h["kind"] == "hire" else "автор статьи"
    out = [f'<b>{e(h["company"])}</b> · {tag} · {e(h["author"][:40])} · {h["pub"][8:10]}.{h["pub"][5:7]} · '
           f'<a href="{e(h["link"])}">пост</a>']
    if apps:
        out.append("Твой отклик: " + e("; ".join(apps[:3])))
    out.append(f"<i>{e(h['head'][:160])}</i>")
    if h["kind"] == "hire" and h["text"]:
        out.append(e(re.sub(r"\s+", " ", h["text"])[:350]))
    roles = {"recruiter": "рекрутер", "lead": "лид/руководитель", "engineer": "инженер (автор)", "other": "роль не указана"}
    for n, (ch, v, role, _) in enumerate(cs[:MAX_BTN], 1):
        out.append(f"{n}. {e(v)} — {roles.get(role, role)}")
    if not cs:
        out.append("Контакта в посте нет — откликнуться по ссылке или искать через job-contacts.")
    return "\n".join(out)


def push(c, hits):
    tg = D.tg_bin()
    if not tg:
        print("WARN: нет tg (tg-bridge) — в Telegram не отправлено", file=sys.stderr)
        return
    for h in hits:
        cs = json.loads(h["contacts"])
        btns = []
        for n in range(1, min(len(cs), MAX_BTN) + 1):
            btns += ["--button", f"{n} в контакты=scout-add:{h['id']}.{n}"]
        btns += ["--button", f"Скип=scout-skip:{h['id']}"]
        p = subprocess.run([tg, "send", "--html", tg_msg(h), *btns], capture_output=True, text=True)
        if p.returncode:
            print(f"WARN: tg send: {p.stderr.strip()}", file=sys.stderr)
            continue
        c.execute("update hits set sent_at=? where id=?", (dt.datetime.now().isoformat(timespec="seconds"), h["id"]))
        c.commit()
        time.sleep(1.1)


# --- команды ---

def cmd_run(a):
    companies, sig = load_companies(), load_signals()
    apps, warn = active_apps(companies)
    if warn:
        print("WARN:", warn, file=sys.stderr)
    c = db()
    known = known_values()
    items = items_from_sources(load_sources(), a.days) + items_from_digest(a.days)
    now = dt.datetime.now().isoformat(timespec="seconds")
    new = 0
    for src, it in items:
        if c.execute("select 1 from seen where guid=?", (it["guid"],)).fetchone():
            continue
        h = analyze(it, src, companies, sig, known)
        if not a.dry_run:
            c.execute("insert or ignore into seen values (?, ?)", (it["guid"], now))
        if not h:
            continue
        if h["kind"] == "author" and h["company"] not in apps:
            continue                                      # авторы — только там, где уже есть отклик
        h_apps = apps.get(h["company"], [])
        if not a.dry_run:
            sight(c, h, it, src, now)
        if h["kind"] == "hire" and not h["contacts"] and h.get("known") and not src.get("company") and src["kind"] != "career":
            continue                                      # агрегатор, где только уже известные люди: встреча записана, слать нечего
        if a.dry_run:
            print(f"[{h['kind']}] {h['company']} | {h['head'][:90]} | {it['link']}\n   отклик: {h_apps[:2]}\n"
                  f"   контакты: {[x[1] + ' (' + x[2] + ')' for x in h['contacts']]}")
            new += 1
            continue
        c.execute("""insert or ignore into hits(guid, source, kind, company, head, text, link, pub, author, contacts, apps,
                     found_at, state) values (?,?,?,?,?,?,?,?,?,?,?,?, 'new')""",
                  (it["guid"], src["url"], h["kind"], h["company"], h["head"], h["text"], h["link"], it.get("pub") or now,
                   h["author"], json.dumps(h["contacts"], ensure_ascii=False), json.dumps(h_apps, ensure_ascii=False), now))
        new += 1
    c.commit()
    print(f"scout: просмотрено {len(items)}, новых находок {new}")
    if a.dry_run or a.no_tg:
        return
    pending = c.execute("select * from hits where sent_at is null and state='new'").fetchall()
    # сначала найм с активным откликом, потом найм с контактом, потом остальное; авторы — в конце
    pending = sorted(pending, key=lambda h: (h["kind"] != "hire", h["company"] == OTHER, h["apps"] in (None, "[]"),
                                             h["contacts"] == "[]"))
    hires = [h for h in pending if h["kind"] == "hire"]
    authors = [h for h in pending if h["kind"] == "author"][:MAX_AUTHORS]
    for h in [h for h in pending if h["kind"] == "author"][MAX_AUTHORS:]:
        c.execute("update hits set state='quiet' where id=?", (h["id"],))   # остаются в show, в TG не идут
    c.commit()
    pending = hires + authors
    push(c, pending[:a.limit])
    if len(pending) > a.limit:
        print(f"scout: ещё {len(pending) - a.limit} находок ждут следующего прогона (лимит {a.limit})")


def cmd_show(a):
    c = db()
    since = (dt.datetime.now() - dt.timedelta(days=a.days)).isoformat()
    q = "select * from hits where found_at >= ?" + ("" if a.all else " and state != 'skipped'") + " order by id"
    for h in c.execute(q, (since,)):
        cs = json.loads(h["contacts"])
        print(f"#{h['id']} [{h['kind']}/{h['state']}] {h['company']} | {h['head'][:80]} | {h['link']}")
        for ch, v, role, _ in cs:
            print(f"     {ch} {v} ({role})")


def get_hit(c, hid):
    h = c.execute("select * from hits where id=?", (int(hid),)).fetchone()
    if not h:
        C.die(f"нет находки #{hid}")
    return h


def cmd_add(a):
    hid, _, k = a.key.partition(".")
    c = db()
    h = get_hit(c, hid)
    cs = json.loads(h["contacts"])
    try:
        ch, v, role, context = cs[int(k) - 1]
    except (ValueError, IndexError):
        C.die(f"в находке #{hid} нет контакта {k}")
    name = h["author"] if h["kind"] == "author" else (v if ch == "tg" else v.split("@")[0])
    note = f"scout {dt.date.today():%d.%m}: {context[:150]}"
    p = subprocess.run([sys.executable, "-I", str(HERE / "contacts.py"), "add", "--company", h["company"],
                        "--name", name, "--role", role, "--channel", ch, "--value", v, "--source", h["link"],
                        "--note", note], capture_output=True, text=True)
    print((p.stdout + p.stderr).strip())
    if p.returncode == 0:
        c.execute("update hits set state='added' where id=?", (h["id"],))
        c.commit()
    sys.exit(p.returncode)


def cmd_skip(a):
    c = db()
    get_hit(c, a.id)
    c.execute("update hits set state='skipped' where id=?", (int(a.id),))
    c.commit()
    print(f"#{a.id}: скип")


def cmd_ctx(a):
    """История встреч человека: где, когда, что было в посте — для первой строки письма «нашёл вас в …»."""
    c = db()
    rows = c.execute("select * from sightings where value=? order by pub", (C.norm(a.value),)).fetchall()
    ct = [r for r in C.read_tsv(C.CONTACTS, C.C_FIELDS) if C.norm(r["value"]) == C.norm(a.value)]
    for r in ct:
        print(f"contacts.tsv: {r['company']} / {r['name']} ({r['role']}), добавлен {r['found_at']}, источник {r['source_url']}")
        if r["note"]:
            print(f"   заметка: {r['note']}")
    for r in rows:
        print(f"{r['pub'][:10]}  {r['company']}  {r['head'][:90]}\n   {r['link']}\n   «{r['context']}»")
    if not rows and not ct:
        print("встреч нет")


def cmd_sources(a):
    for s in load_sources():
        try:
            name, items = D.parse_feed(D.fetch(s["url"]))
            print(f"OK   {len(items):3d}  {s['kind']:6s} {name[:50]}  {s['url']}")
        except Exception as e:
            print(f"FAIL      {s['kind']:6s} {s['url']}: {e}")
    print("+ ленты дайджеста из", DIGEST_DB, "(есть)" if DIGEST_DB.exists() else "(нет)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="напечатать находки, ничего не сохранять и не слать")
    r.add_argument("--no-tg", action="store_true")
    r.add_argument("--days", type=int, default=3, help="возраст постов (по умолч. 3 дня)")
    r.add_argument("--limit", type=int, default=15, help="не больше N сообщений в Telegram за прогон")
    s = sp.add_parser("show")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--all", action="store_true")
    sp.add_parser("sources")
    sp.add_parser("add").add_argument("key")
    sp.add_parser("skip").add_argument("id")
    sp.add_parser("ctx", help="где и в каком контексте встречался человек").add_argument("value")
    a = ap.parse_args()
    {"run": cmd_run, "show": cmd_show, "sources": cmd_sources, "add": cmd_add, "skip": cmd_skip, "ctx": cmd_ctx}[a.cmd](a)


if __name__ == "__main__":
    main()
