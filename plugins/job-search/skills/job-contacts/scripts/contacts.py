#!/usr/bin/env python3
"""Публичные рабочие контакты рекрутеров и тимлидов: извлечь, сохранить с источником, показать карту, follow-up.

  contacts.py extract <url|file|->          # кандидаты: email, @handle, t.me/…, телефоны рядом с HR-словами
  contacts.py add --company C --name N --role R --channel email|tg|phone|linkedin|other --value V --source URL
                  [--team T] [--note TXT]
  contacts.py map [company]                 # дерево компания → команда → люди
  contacts.py due [--today YYYY-MM-DD] [--days N]   # кому пора написать / напомнить
  contacts.py log <value> sent|followup|replied|bounced|skip [--note TXT]

Данные: $JOB_APPLY_HOME (по умолчанию ~/.local/share/job-apply): contacts.tsv, outreach.tsv, applied.tsv.
Правило: контакт без source_url не сохраняется. Только то, что человек сам опубликовал как рабочее.
"""
import argparse, csv, datetime as dt, html, os, re, sys, urllib.request
from pathlib import Path

HOME = Path(os.environ.get("JOB_APPLY_HOME", "~/.local/share/job-apply")).expanduser()
CONTACTS, OUTREACH, APPLIED = HOME / "contacts.tsv", HOME / "outreach.tsv", HOME / "applied.tsv"
C_FIELDS = ["company", "team", "name", "role", "channel", "value", "source_url",
            "found_at", "status", "last_contact_at", "note"]
O_FIELDS = ["date", "company", "name", "channel", "value", "action", "note"]
ROLES = {"recruiter", "hr-brand", "hiring-manager", "lead", "engineer", "other"}
CHANNELS = {"email", "tg", "phone", "linkedin", "other"}
CLOSED = re.compile(r"закрыт|отказ|closed|reject", re.I)
FOLLOWUP_DAYS = 7          # между первым письмом и напоминанием
MAX_TOUCHES = 2            # первое сообщение + одно напоминание

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-zа-я]{2,}", re.I)
TG = re.compile(r"(?:t\.me/|telegram\.me/)([A-Za-z][\w]{4,31})|(?<![\w@.])@([A-Za-z][\w]{4,31})\b")
PHONE = re.compile(r"(?:\+7|\b8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b")
HR_WORDS = re.compile(r"рекрут|recruit|hr\b|эйчар|кадр|стажир|intern|talent|подбор|карьер|career|вакан", re.I)
TG_SKIP = {"gmail", "mail", "yandex", "media", "import", "keyframes", "font", "charset", "supports", "type"}


def die(msg):
    print(f"ERR: {msg}", file=sys.stderr)
    sys.exit(1)


def read_tsv(path, fields):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return [dict(zip(fields, row + [""] * (len(fields) - len(row))))
                for row in csv.reader(f, delimiter="\t") if row and not row[0].startswith("#")]


def write_tsv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        for r in rows:
            w.writerow([r.get(k, "") for k in fields])


def append_tsv(path, fields, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as f:
        csv.writer(f, delimiter="\t", lineterminator="\n").writerow([row.get(k, "") for k in fields])


def today(a=None):
    return dt.date.fromisoformat(a) if a else dt.date.today()


def norm(s):
    return re.sub(r"[\W_]+", "", (s or "").lower().replace("ё", "е"))


# --- extract ---

def load_text(src):
    if src == "-":
        return sys.stdin.read()
    if re.match(r"https?://", src):
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 job-contacts"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
    else:
        raw = Path(src).read_text(encoding="utf-8", errors="replace")
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    raw = re.sub(r'(?i)href="mailto:([^"?]+)', r" \1 ", raw)       # mailto без видимого текста
    raw = re.sub(r'(?i)href="(https?://t\.me/[^"]+)"', r" \1 ", raw)
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def ctx(text, m, width=90):
    s = text[max(0, m.start() - width):m.end() + width]
    return re.sub(r"\s+", " ", s).strip()


def cmd_extract(a):
    try:
        text = load_text(a.src)
    except Exception as e:
        die(f"не прочитать {a.src}: {e}. Для JS-страниц: browser.mjs text 20000 > page.txt; extract page.txt")
    seen, out = set(), []
    for m in EMAIL.finditer(text):
        v = m.group(0).lower().rstrip(".")
        if v.endswith((".png", ".jpg", ".svg", ".webp")) or v in seen:
            continue
        seen.add(v); out.append(("email", v, ctx(text, m)))
    for m in TG.finditer(text):
        h = (m.group(1) or m.group(2))
        if h.lower() in TG_SKIP or ("@" + h.lower()) in seen or any(h.lower() in e for e in seen if "@" in e and not e.startswith("@")):
            continue
        seen.add("@" + h.lower()); out.append(("tg", "@" + h, ctx(text, m)))
    for m in PHONE.finditer(text):
        c = ctx(text, m, 140)
        if not HR_WORDS.search(c):            # телефон берём только в HR-контексте
            continue
        v = re.sub(r"[^\d+]", "", m.group(0))
        if v in seen:
            continue
        seen.add(v); out.append(("phone", v, c))
    if not out:
        print("контактов не найдено")
    for ch, v, c in out:
        print(f"{ch}\t{v}\t…{c}…")
    print(f"\n# источник: {a.src}\n# сохранить: contacts.py add --company … --name … --role … "
          f"--channel … --value … --source '{a.src}'", file=sys.stderr)


# --- add / map ---

def cmd_add(a):
    if not a.source or not re.match(r"https?://", a.source):
        die("--source обязателен и должен быть URL страницы, где контакт опубликован")
    if a.role not in ROLES:
        die(f"--role: одно из {sorted(ROLES)}")
    if a.channel not in CHANNELS:
        die(f"--channel: одно из {sorted(CHANNELS)}")
    value = a.value.strip()
    if a.channel == "email":
        value = value.lower()
        if not EMAIL.fullmatch(value):
            die(f"не похоже на email: {value}")
    if a.channel == "tg":
        value = "@" + value.lstrip("@").split("t.me/")[-1].strip("/")
    rows = read_tsv(CONTACTS, C_FIELDS)
    for r in rows:
        if r["value"].lower() == value.lower():
            print(f"уже есть: {r['company']} / {r['name']} / {r['role']} ({r['source_url']})")
            return
    same_team = [r for r in rows if norm(r["company"]) == norm(a.company) and norm(r["team"]) == norm(a.team or "")
                 and r["name"] != a.name]
    if len({r["name"] for r in same_team}) >= 2:
        print(f"⚠ в команде «{a.team or '—'}» уже 2+ человека: писать стоит не больше двум", file=sys.stderr)
    row = dict(company=a.company, team=a.team or "", name=a.name, role=a.role, channel=a.channel, value=value,
               source_url=a.source, found_at=today().isoformat(), status="new", last_contact_at="", note=a.note or "")
    append_tsv(CONTACTS, C_FIELDS, row)
    print(f"OK: {a.company} / {a.team or '—'} / {a.name} ({a.role}) {a.channel} {value}")
    if a.channel == "email":
        dom = value.split("@", 1)[1]
        print(f"не забудь: домен {dom} → «Отклики» в ~/.config/job-digest/mail-rules.json и в фильтре mail.ru",
              file=sys.stderr)


def cmd_map(a):
    rows = read_tsv(CONTACTS, C_FIELDS)
    if a.company:
        rows = [r for r in rows if norm(a.company) in norm(r["company"])]
    if not rows:
        print("пусто"); return
    tree = {}
    for r in rows:
        tree.setdefault(r["company"], {}).setdefault(r["team"] or "—", []).append(r)
    order = {"recruiter": 0, "hr-brand": 1, "hiring-manager": 2, "lead": 3, "engineer": 4, "other": 5}
    for comp, teams in tree.items():
        print(comp)
        for team, people in teams.items():
            print(f"  {team}")
            for r in sorted(people, key=lambda r: order.get(r["role"], 9)):
                last = f", писал {r['last_contact_at']}" if r["last_contact_at"] else ""
                print(f"    {r['role']:<15} {r['name']:<28} {r['channel']:<6} {r['value']:<36} [{r['status']}{last}]")
                print(f"    {'':15} источник: {r['source_url']}")


# --- due / log ---

def person_of(value):
    """Строка контакта по значению канала (email/@handle/…)."""
    for r in read_tsv(CONTACTS, C_FIELDS):
        if r["value"].lower() == value.lower():
            return r
    return None


def touches(value):
    """Касания человека по всем его каналам: лимит считается на человека, а не на адрес."""
    p = person_of(value)
    key = (norm(p["company"]), norm(p["name"])) if p else None
    return [o for o in read_tsv(OUTREACH, O_FIELDS) if o["action"] in ("sent", "followup") and
            ((key and (norm(o["company"]), norm(o["name"])) == key) or o["value"].lower() == value.lower())]


def check_touch(value, followup, now=None):
    """None, если можно писать; иначе причина отказа."""
    now = now or dt.date.today()
    t = touches(value)
    if len(t) >= MAX_TOUCHES:
        return f"уже {len(t)} касания этого человека — лимит {MAX_TOUCHES}, дальше только если ответят"
    if t:
        last = max(dt.date.fromisoformat(o["date"]) for o in t)
        if (now - last).days < FOLLOWUP_DAYS:
            return f"писали {last} — напоминание не раньше чем через {FOLLOWUP_DAYS} дн."
        if not followup:
            return "это второе касание — помечай как followup"
    return None


def cmd_due(a):
    now, contacts = today(a.today), read_tsv(CONTACTS, C_FIELDS)
    by_company = {}
    for r in contacts:
        by_company.setdefault(norm(r["company"]), []).append(r)

    def match(company):
        k = norm(company)
        return [r for ck, rs in by_company.items() if ck and (ck in k or k in ck) for r in rs]

    print(f"# {now}: follow-up")
    any_ = False
    # 1) напоминания тем, кому уже писали и кто молчит (по человеку, не по каналу)
    done = set()
    for r in contacts:
        pk = (norm(r["company"]), norm(r["name"]))
        if pk in done:
            continue
        done.add(pk)
        t = touches(r["value"])
        if not t or r["status"] in ("replied", "bounced", "skip"):
            continue
        last = max(dt.date.fromisoformat(o["date"]) for o in t)
        if len(t) < MAX_TOUCHES and (now - last).days >= FOLLOWUP_DAYS:
            any_ = True
            print(f"напомнить  {r['company']} / {r['name']} ({r['role']}) {r['channel']} {r['value']} — "
                  f"молчит {(now - last).days} дн. с {last}")
    # 2) отклики старше N дней без ответа — первый контакт, если есть кому
    for row in read_tsv(APPLIED, ["date", "company", "title", "url", "resume", "status"]):
        try:
            d = dt.date.fromisoformat(row["date"])
        except ValueError:
            continue
        if CLOSED.search(row["status"]) or (now - d).days < a.days:
            continue
        known = match(row["company"])
        if any(touches(r["value"]) for r in known):
            continue                      # в компанию уже писали — дальше это «напомнить» выше
        people = [r for r in known if r["status"] not in ("bounced", "skip")]
        any_ = True
        per = {}
        for r in people:
            per.setdefault(r["name"], []).append(f"{r['channel']} {r['value']}")
        who = "; ".join(f"{n} — {', '.join(ch)}" for n, ch in list(per.items())[:2]) \
            or "контактов нет → найти (SKILL.md, шаг 2)"
        print(f"написать   {row['company']} — «{row['title']}», отклик {d} ({(now - d).days} дн.): {who}")
    if not any_:
        print("ничего не горит")


def cmd_log(a):
    rows = read_tsv(CONTACTS, C_FIELDS)
    hit = [r for r in rows if r["value"].lower() == a.value.lower()]
    if not hit:
        die(f"нет контакта {a.value}; сначала add")
    r = hit[0]
    if a.action in ("sent", "followup") and not getattr(a, "checked", False):
        why = check_touch(r["value"], a.action == "followup")
        if why:
            die(f"{r['value']}: {why}")
    stamp = today().isoformat()
    append_tsv(OUTREACH, O_FIELDS, dict(date=stamp, company=r["company"], name=r["name"], channel=r["channel"],
                                        value=r["value"], action=a.action, note=a.note or ""))
    new = {"sent": "contacted", "followup": "contacted"}.get(a.action, a.action)
    for x in rows:                       # статус и дата — на все каналы этого человека
        if norm(x["company"]) == norm(r["company"]) and norm(x["name"]) == norm(r["name"]):
            x["status"] = new
            if a.action in ("sent", "followup"):
                x["last_contact_at"] = stamp
    write_tsv(CONTACTS, C_FIELDS, rows)
    print(f"OK: {r['name']} → {r['status']}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract"); e.add_argument("src"); e.set_defaults(f=cmd_extract)
    ad = sub.add_parser("add")
    for k in ("company", "name", "role", "channel", "value"):
        ad.add_argument(f"--{k}", required=True)
    ad.add_argument("--source"); ad.add_argument("--team"); ad.add_argument("--note")
    ad.set_defaults(f=cmd_add)
    m = sub.add_parser("map"); m.add_argument("company", nargs="?"); m.set_defaults(f=cmd_map)
    d = sub.add_parser("due"); d.add_argument("--today"); d.add_argument("--days", type=int, default=10)
    d.set_defaults(f=cmd_due)
    lg = sub.add_parser("log"); lg.add_argument("value")
    lg.add_argument("action", choices=["sent", "followup", "replied", "bounced", "skip"]); lg.add_argument("--note")
    lg.set_defaults(f=cmd_log)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
