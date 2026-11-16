#!/usr/bin/env python3
"""Короткие сводки для команд бота (читаются с телефона, без ссылок и простыней).

  board.py queue      /queue    — очередь автооткликов по статусам: что ждёт и почему
  board.py tracker    /tracker  — открытые строки job_tracker: что требует действия, что ждёт ответа
  board.py contacts   /contacts — контакты рекрутеров: кто, где, писал ли
"""
import argparse, re, sys, time
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
    rows, ts = S.cached_sheet()
    if rows is None or time.time() - ts > 600:
        try:
            rows = S.fetch_sheet() or rows
        except Exception as e:                                     # нет сети — по кэшу
            print(f"(таблица не ответила, данные на {time.strftime('%d.%m %H:%M', time.localtime(ts))})\n"
                  if rows else f"таблица недоступна: {e}")
    return rows or []


def cmd_tracker(a):
    rows = [r for r in sheet() if len(r["values"]) > 4 and r["values"][0] and r["values"][4]]
    opened = [r for r in rows if r["values"][4] != "Закрыто"]
    groups = {}
    for r in opened:
        groups.setdefault(r["values"][4], []).append(r)
    order = sorted(groups, key=lambda s: (0 if "ДЕЙСТВ" in s.upper() else 1 if s == "В процессе" else 2, s))
    out = [f"Трекер: открыто {len(opened)}, закрыто {len(rows) - len(opened)}"]
    for st in order:
        out.append(f"\n{st} ({len(groups[st])})")
        for r in groups[st]:
            v = r["values"] + [""] * 6
            hot = due(v[5]) or st != "Ожидание ответа"
            out.append(f"  {'⏰' if due(v[5]) else '#'}{r['row']} {short(v[0], 28)} — {short(v[1], 40)}")
            if hot and v[5] and v[5] != "-":                  # шаг показываем, только когда пора
                out.append(f"      → {short(v[5], 85)}")
    out.append("\n⏰ — дата следующего шага наступила; у остальных шаг в трекере, столбец F")
    print("\n".join(out)[:3400])


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
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
