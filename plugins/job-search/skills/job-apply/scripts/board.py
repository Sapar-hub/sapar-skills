#!/usr/bin/env python3
"""Сводки для команд бота: читаются с телефона, без простыней адресов и без эмодзи — жирный/курсив и toggle.

  board.py queue [--tg]      /queue    — очередь автооткликов по статусам: что ждёт и почему
  board.py tracker [--tg]    /tracker  — открытые строки job_tracker карточками (подробности свёрнуты)
  board.py contacts [--tg]   /contacts — контакты рекрутеров: кто, где, писал ли

--tg: одним rich-сообщением в Telegram (`tg send --rich`): Markdown, каждая запись — сворачиваемый
<details>-блок (заголовок виден, подробности по нажатию), до 32768 символов.
Без --tg — короткий текст для консоли.
"""
import argparse, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import status as S  # noqa: E402

QUEUE_ORDER = [("paused", "Ждут тебя"), ("manual", "Заполнить руками"), ("in-progress", "В работе"),
               ("new", "В очереди"), ("failed", "Ошибка"), ("bl-review", "Проверка")]
CONTACT_STATUS = {"new": "не писал", "contacted": "писал", "replied": "ответил", "bounced": "адрес не работает",
                  "skip": "пропуск"}


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


def md(s):
    """Текст из таблицы/очереди -> безопасный для Markdown (звёздочки, подчёркивания, ~ и т.п. — буквально)."""
    return re.sub(r"([\\`*_\[\]<>|~#])", r"\\\1", re.sub(r"\s+", " ", s or "").strip())


def emit_rich(a, text):
    """--tg: одним rich-сообщением (toggle-блоки <details>); консоль — без разметки."""
    if not a.tg:
        plain = re.sub(r"</?(details|summary)[^>]*>", "", text)
        print(re.sub(r"\\(.)", r"\1", re.sub(r"[*]{1,3}|^#+ ", "", plain, flags=re.M)))
        return
    r = subprocess.run(["tg", "send", "--rich", "--stdin"], input=text[:32000], capture_output=True, text=True)
    if r.returncode:
        print(f"ERR tg send: {r.stderr.strip()[:300]}")


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
    out = ["**Очередь автооткликов**"]
    for st, title in QUEUE_ORDER:
        items = [r for r in rows if r["status"] == st]
        if not items:
            continue
        out.append(f"\n## {title} ({len(items)})\n")
        for r in items:
            link = r.get("href") or r.get("post") or ""
            name = f"[{md(short(r['title'], 80))}]({link})" if link.startswith("http") else md(short(r["title"], 80))
            note = re.sub(r"^[\s—-]+", "", r.get("note", "") or "")
            why = f"\n  *{md(reason(note))}*" if st != "new" and note else ""
            out.append(f"- `{r['key']}` {name}{why}")             # списком, причина курсивом под пунктом
    emit_rich(a, "\n".join(out) if len(out) > 1 else "Очередь пуста.")


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
    """Вакансия — toggle: заголовок виден, подробности по нажатию."""
    v = r["values"] + [""] * 14
    company, title, date, stage, status, nxt, _, notes, url = v[:9]
    head = f"**#{r['row']} {md(company)}** — {md(title)}" + (" · ***пора***" if due(nxt) else "")
    body = [f"*{md(stage or 'Отклик')} {md(ddmm(date))} · {md(status)}*"]
    if nxt and nxt != "-":
        body.append(f"**Дальше:** {md(nxt)}")
    if notes:
        body.append(md(notes))
    link = next((u for u in re.split(r"\s*;\s*|\s+", url) if u.startswith("http")), "")
    if link:
        body.append(f"[вакансия]({link})")
    return f"<details><summary>{head}</summary>\n\n" + "\n\n".join(body) + "\n</details>\n"


def cmd_tracker(a):
    data, src = sheet()
    rows = [r for r in data if len(r["values"]) > 4 and r["values"][0] and r["values"][4]]
    opened = [r for r in rows if r["values"][4] != "Закрыто"]
    groups = {}
    for r in opened:
        groups.setdefault(r["values"][4], []).append(r)
    order = sorted(groups, key=lambda s: (0 if "ДЕЙСТВ" in s.upper() else 1 if s == "В процессе" else 2, s))
    ndue = sum(due((r["values"] + [""] * 6)[5]) for r in opened)
    out = [f"**Трекер**: открыто {len(opened)}, закрыто {len(rows) - len(opened)}, пора действовать — {ndue}",
           f"*{md(src)}*"]
    for st in order:
        items = sorted(groups[st], key=lambda r: (not due((r["values"] + [""] * 6)[5]), r["row"]))
        out.append(f"\n## {md(st)} ({len(items)})\n")
        out += [card(r) for r in items]
    emit_rich(a, "\n".join(out))


# --- /contacts ---

def cmd_contacts(a):
    sys.path.insert(0, str(HERE.parent.parent / "job-contacts" / "scripts"))
    import contacts as C
    rows = C.read_tsv(C.CONTACTS, C.C_FIELDS)
    if not rows:
        emit_rich(a, "Контактов нет."); return
    by = {}
    for r in rows:
        by.setdefault(r["company"], []).append(r)
    out = [f"**Контакты**: {len(rows)} в {len(by)} компаниях\n"]
    for comp, people in by.items():
        wrote = sum(p["status"] in ("contacted", "replied") for p in people)
        replied = sum(p["status"] == "replied" for p in people)
        summary = f"**{md(comp)}** — {len(people)} · *писал {wrote}, ответили {replied}*"
        lines = []
        for r in people:
            st = CONTACT_STATUS.get(r["status"], r["status"])
            when = f" {ddmm(r['last_contact_at'])}" if r["last_contact_at"] else ""
            srcl = f" · [источник]({r['source_url']})" if r.get("source_url", "").startswith("http") else ""
            note = f"\n  {md(r['note'])}" if r.get("note") else ""
            lines.append(f"- **{md(r['name'])}** *({md(r['role'])})* — `{r['value']}` · *{md(st)}{when}*{srcl}{note}")
        out.append(f"<details><summary>{summary}</summary>\n\n" + "\n".join(lines) + "\n</details>\n")
    emit_rich(a, "\n".join(out))


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
