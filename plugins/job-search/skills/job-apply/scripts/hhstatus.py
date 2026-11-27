#!/usr/bin/env python3
"""Статусы откликов с hh.ru → job_tracker. Источник: hh.mjs negotiations (страница «Отклики» соискателя).

  hhstatus.py            показать, что изменилось бы (лист не трогает)
  hhstatus.py --apply    записать в лист (sheet.py web app) и запомнить состояние
  hhstatus.py --apply --tg   то же + сводка в Telegram, если есть изменения (для таймера)

Связь отклика hh со строкой листа — только по id вакансии hh в колонке I (ссылка) или N (переписка).
Без такой ссылки строка не трогается, отклик попадает в «нет в трекере».

Что пишется в открытые строки (E = Ожидание ответа / В процессе):
  rejection   → E=Закрыто, F=-, D += «; отказ hh <дата>», N += строка
  invitation  → E=В процессе, F=⚠ ДЕЙСТВИЕ НУЖНО: приглашение на hh — ответить в чате, N += строка
  viewed      → N += «hh <дата>: резюме просмотрено» (один раз)
Закрытые строки не меняются; приглашение по закрытой строке — только в сводку.
Состояние (что уже записано): $JOB_APPLY_HOME/hh-status.json.
"""
import argparse, json, os, re, subprocess, sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("JOB_APPLY_HOME", "~/.local/share/job-apply")).expanduser()
STATE = HOME / "hh-status.json"
OPEN = {"Ожидание ответа", "В процессе"}
MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october november december".split())}
MONTHS.update({m: i + 1 for i, m in enumerate(
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split())})
LABEL = {"rejection": "отказ", "invitation": "приглашение", "viewed": "резюме просмотрено",
         "not-viewed": "не просмотрено"}


def hh_date(s, today):
    s = (s or "").strip().lower()
    if s in ("today", "сегодня"):
        return today
    if s in ("yesterday", "вчера"):
        return today - timedelta(days=1)
    m = re.match(r"(\d{1,2})\s+([a-zа-яё]+)(?:\s+(\d{4}))?", s)
    if m and m.group(2) in MONTHS:
        d = date(int(m.group(3) or today.year), MONTHS[m.group(2)], int(m.group(1)))
        return d.replace(year=d.year - 1) if d > today else d
    return None


def negotiations():
    env = dict(os.environ)
    # браузер job-apply мог быть не запущен (таймер): поднимаем окном на невидимом Xvfb, не headless —
    # иначе воркер потом подцепится к headless-браузеру, а job.psbank.ru его режет; уже запущенный не трогается
    subprocess.run(["node", str(HERE / "browser.mjs"), "start", "--offscreen"], cwd=HERE, env=env,
                   capture_output=True, timeout=120)
    r = subprocess.run(["node", str(HERE / "hh.mjs"), "negotiations", "--json"], capture_output=True,
                       text=True, cwd=HERE, env=env, timeout=600)
    if r.returncode:
        sys.exit(f"ERR hh.mjs: {(r.stderr or r.stdout).strip()[-500:]}")
    return json.loads(r.stdout)


# «Приглашение» на hh иногда ставит робот работодателя вместе с шаблоном «отклик получен» (МТС, 27.11):
# такое не помечаем «⚠ ДЕЙСТВИЕ НУЖНО», а только пишем в N. Настоящее приглашение — со словами про встречу.
AUTOREPLY = re.compile(r"получен и направлен|процесс отбора .{0,40}может занять|мы свяжемся|рассмотрим ваш[ае]? (отклик|резюме)|"
                       r"спасибо за (ваш )?отклик|голос за", re.I)
REAL_INVITE = re.compile(r"собеседован|интервью (?!с ии)|созвон|встреч|тестов|zoom|телемост|meet\.|ссылк|"
                         r"удобн\w* (время|дат)|в \d{1,2}[:.]\d{2}|завтра|понедельник|вторник|сред[ау]|четверг|пятниц", re.I)


def invite_is_autoreply(vacancy_id):
    """True — в чате только шаблон-автоответ без признаков встречи; при любой ошибке False (пусть человек посмотрит)."""
    try:
        r = subprocess.run(["node", str(HERE / "hh.mjs"), "chat", vacancy_id], capture_output=True, text=True,
                           cwd=HERE, timeout=180)
        tail = json.loads(r.stdout).get("tail", "")
    except Exception:
        return False
    return bool(AUTOREPLY.search(tail)) and not REAL_INVITE.search(tail)


def vid(u):
    m = re.search(r"hh\.ru/vacancy/(\d+)", u or "")
    return m.group(1) if m else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--tg", action="store_true")
    a = ap.parse_args()
    sys.path.insert(0, str(HERE))
    import sheet
    if not sheet.conf():
        sys.exit("ERR: sheet.py не настроен (~/.config/job-apply/sheet.env)")

    today = date.today()
    negs = negotiations()
    rows = sheet.call("find", query="", limit=1000)["rows"]
    by_id = {}
    for r in rows:
        v = r["values"]
        for i in set(re.findall(r"hh\.ru/vacancy/(\d+)", v[8] + " " + v[13])):
            by_id.setdefault(i, []).append(r)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    changes, notes, unmatched = [], [], []
    for n in negs:
        i = vid(n["url"])
        st = n["status"]
        d = hh_date(n["date"], today)          # дата отклика на hh (не дата смены статуса)
        ds = today.strftime("%d.%m")             # когда увидели статус
        if not i:
            continue
        targets = by_id.get(i, [])
        if not targets:
            if st == "invitation" or (d and (today - d).days <= 14):
                unmatched.append(f"{n['company']} — {n['title']} ({LABEL.get(st, st)}; отклик {n['date']}) {n['url']}")
            state[i] = st
            continue
        if state.get(i) == st:
            continue
        for r in targets:
            v = r["values"]
            row, is_open = r["row"], v[4] in OPEN
            name = f"#{row} {v[0]} — {v[1][:60]}"
            line = f"hh {ds}: {LABEL.get(st, st)}"
            if line in v[13]:
                continue
            if st == "rejection" and is_open:
                d_col = v[3] if "отказ" in v[3].lower() else f"{v[3]}; отказ hh {ds}".lstrip("; ")
                changes.append((row, {"E": "Закрыто", "F": "-", "D": d_col}, {"N": line}, f"{name}: отказ на hh ({ds}) → закрыто"))
            elif st == "invitation" and is_open and invite_is_autoreply(i):
                changes.append((row, {}, {"N": f"hh {ds}: «приглашение» — автоответ работодателя, встречи не назначено"},
                                f"{name}: «приглашение» на hh ({ds}) — автоответ, не трогаю"))
            elif st == "invitation" and is_open:
                changes.append((row, {"E": "В процессе", "F": f"⚠ ДЕЙСТВИЕ НУЖНО: приглашение на hh {ds} — ответить в чате hh {n['url']}"},
                                {"N": line}, f"{name}: ПРИГЛАШЕНИЕ на hh ({ds})"))
            elif st == "invitation":
                notes.append(f"{name}: приглашение на hh ({ds}), а строка закрыта ({v[4]}) — посмотри руками")
            elif st == "viewed" and is_open:
                changes.append((row, {}, {"N": line}, f"{name}: резюме просмотрено на hh ({ds})"))
        state[i] = st

    out = []
    if changes:
        out.append("Изменения из hh:")
        out += [f"- {c[3]}" for c in changes]
    if notes:
        out.append("Посмотреть:")
        out += [f"- {x}" for x in notes]
    if unmatched:
        out.append("Отклики hh без строки в трекере (нет ссылки hh в I/N):")
        out += [f"- {x}" for x in unmatched]
    if not out:
        out = [f"hh: без изменений ({len(negs)} откликов)"]
    print("\n".join(out))

    if not a.apply:
        print("\n(сухой прогон; --apply запишет)")
        return
    for row, set_, add, _ in changes:
        sheet.call("update", row_number=row, set=set_, append=add)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    (HOME / "sheet-cache.json").unlink(missing_ok=True)       # status.py перечитает лист
    if a.tg and (changes or notes):
        subprocess.run(["tg", "send", "\n".join(out)], check=False, timeout=60)


if __name__ == "__main__":
    main()
