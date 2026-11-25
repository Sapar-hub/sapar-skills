#!/usr/bin/env python3
"""Закрыть отклик, если на наше последнее касание (письмо, напоминание, чат hh) не ответили за --days дней.

  autoclose.py               показать, что закрылось бы (лист не трогает)
  autoclose.py --apply       закрыть в листе (sheet.py web app)
  autoclose.py --apply --tg  то же + сводка в Telegram (для таймера)

Касания — outreach.tsv (send.py, contacts.py log, hhchat.py). Строка трекера берётся из пометки «rows=45,48» в note
(send.py --rows, hhchat.py пишет сам); у записи чата hh без пометки — по id вакансии hh в I/N. Письмо без rows= —
только если у компании ровно одна открытая строка, иначе в сводку «посмотри руками».
Человек ответил (outreach answered/replied после касания, или статус контакта replied) — не закрываем.
Закрытие: E = Закрыто, D += «; без ответа N дн. после <канал> dd.mm», F = -, N += строка. Только E = «Ожидание ответа»:
«В процессе» (было интервью, приглашение) автоматом не закрываем — там нужен человек.
"""
import argparse, re, subprocess, sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import status as S  # noqa: E402

OUTREACH = S.HOME / "outreach.tsv"
CONTACTS = S.HOME / "contacts.tsv"
TOUCH = {"sent", "followup"}
REPLY = {"answered", "replied"}


def norm(s):
    return re.sub(r"[^\wа-яё]+", " ", (s or "").lower()).strip()


def outreach():
    rows = []
    if OUTREACH.exists():
        for line in OUTREACH.read_text(encoding="utf-8").splitlines():
            f = (line.split("\t") + [""] * 7)[:7]
            if re.match(r"\d{4}-\d{2}-\d{2}$", f[0]):
                rows.append(dict(zip(["date", "company", "name", "channel", "value", "action", "note"], f)))
    return rows


def replied_contacts():
    out = set()
    if CONTACTS.exists():
        for line in CONTACTS.read_text(encoding="utf-8").splitlines():      # без заголовка
            f = line.split("\t")
            if len(f) > 8 and f[8] == "replied":
                out.add(f[5].lower())
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14, help="дней без ответа после последнего касания (14)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--tg", action="store_true")
    a = ap.parse_args()

    sheet_rows = S.fetch_sheet()
    if sheet_rows is None:
        sys.exit("ERR: sheet.py не настроен")
    today = date.today()
    by_row = {r["row"]: (r["values"] + [""] * 14) for r in sheet_rows}
    # F с будущей датой («~30.12: ждать») — запланированное ожидание, не игнор: такие строки не трогаем
    open_rows = {n: v for n, v in by_row.items() if v[4] == "Ожидание ответа"
                 and not ((d := S.due_date(v[5], today)) and d > today)}
    by_vid = {}
    for n, v in open_rows.items():
        for i in set(re.findall(r"hh\.ru/vacancy/(\d+)", v[8] + " " + v[13])):
            by_vid.setdefault(i, []).append(n)

    # последнее касание и последний ответ по каждому контакту
    last_touch, last_reply = {}, {}
    for o in outreach():
        k = o["value"].lower()
        if o["action"] in TOUCH:
            last_touch[k] = o
        elif o["action"] in REPLY:
            last_reply[k] = o["date"]
    gone = replied_contacts()

    close, manual = {}, []
    for k, o in last_touch.items():
        if last_reply.get(k, "") >= o["date"] or k in gone:
            continue
        silent = (today - date.fromisoformat(o["date"])).days
        if silent < a.days:
            continue
        m = re.search(r"rows=([\d,]+)", o["note"])
        if m:
            targets = [int(x) for x in m.group(1).split(",") if x]
        elif o["channel"] == "hh-chat":
            targets = by_vid.get(o["value"], [])
        else:
            same = [n for n, v in open_rows.items() if norm(o["company"]) and
                    (norm(o["company"]) in norm(v[0]) or norm(v[0]) in norm(o["company"]))]
            if len(same) > 1:
                manual.append(f"{o['company']} / {o['name']} молчит {silent} дн.: строк компании {len(same)} "
                              f"({', '.join('#' + str(n) for n in same)}) — какую закрыть? (send.py --rows в следующий раз)")
                continue
            targets = same
        how = "чата hh" if o["channel"] == "hh-chat" else "письма"
        for n in targets:
            if n in open_rows and n not in close:
                close[n] = (silent, how, o)

    out = []
    import sheet
    for n, (silent, how, o) in sorted(close.items()):
        v = open_rows[n]
        d0 = date.fromisoformat(o["date"])
        out.append(f"#{n} {v[0]} — {v[1][:60]}: без ответа {silent} дн. после {how} {d0:%d.%m} ({o['name']}) → закрыто")
        if a.apply:
            sheet.call("update", row_number=n,
                       set={"E": "Закрыто", "F": "-", "D": f"{v[3]}; без ответа {silent} дн. после {how} {d0:%d.%m}"},
                       append={"N": f"{today:%d.%m} autoclose: ответа нет {silent} дн. после {how} {d0:%d.%m}"})
    out += [f"посмотри: {m}" for m in manual]
    print("\n".join(out) or f"нечего закрывать: все касания моложе {a.days} дн. или с ответом")
    if not a.apply:
        print("\n(сухой прогон; --apply закроет)")
    if a.tg and a.apply and out:
        subprocess.run(["tg", "send", "Автозакрытие откликов (игнор):\n" + "\n".join(out)], timeout=60)


if __name__ == "__main__":
    main()
