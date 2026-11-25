#!/usr/bin/env python3
"""Напоминание работодателю в чате hh по отклику, на который молчат.

  hhchat.py                 показать, кому написали бы (браузер читает чаты, ничего не шлёт)
  hhchat.py --apply         написать в открытые чаты (через гейт в Telegram) и записать в трекер / outreach.tsv
  hhchat.py --apply --tg    то же + сводка в Telegram (для таймера)

Кого берём: строка трекера открыта (E = «Ожидание ответа»), в I или N есть ссылка на вакансию hh, со дня отклика (C)
прошло ≥ --days рабочих дней (5), в этот чат мы ещё не писали (outreach.tsv, канал hh-chat). Одно сообщение на вакансию.
Пропускаем: F с будущей датой (запланированное ожидание) и строки, которым за 7 дней ушло письмо (rows= в outreach).
Чат открыт = у переписки есть поле ввода (hh.mjs chat). Закрыт («переписка будет доступна после приглашения») — только
отметка в N и outreach, больше эту вакансию не трогаем.

Гейт: tg ask с текстом и кнопками «отправить / нет»; нет ответа за --timeout секунд (900) — отправляем (так решил
Сапар 25.11 для гейтов job-apply). Без tg — только сухой прогон.
После отправки: N += «dd.mm написал в чат hh», F = «<дата+7>: ответа в чате нет → autoclose закроет». Закрывает
молчащие отклики autoclose.py — по записи hh-chat в outreach.tsv.
"""
import argparse, json, re, shutil, subprocess, sys, tempfile
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import status as S  # noqa: E402

OUTREACH = S.HOME / "outreach.tsv"
TEXT = ("Добрый день! {date} откликнулся на вакансию «{title}». Подскажите, пожалуйста, рассматривается ли ещё "
        "моя кандидатура? Если нужно, готов выполнить тестовое задание.\nСапар")


def workdays_since(d, today):
    n, x = 0, d
    while x < today:
        x += timedelta(days=1)
        n += x.weekday() < 5
    return n


def chatted():
    """id вакансий, по которым уже есть запись hh-chat в outreach.tsv."""
    if not OUTREACH.exists():
        return set()
    out = set()
    for line in OUTREACH.read_text(encoding="utf-8").splitlines():
        f = line.split("\t")
        if len(f) >= 6 and f[3] == "hh-chat":
            out.add(f[4])
    return out


def touched_rows(today, days=7):
    """строки трекера, которым уже писали (rows= в outreach.tsv) за последние days дней — не дублируем касание."""
    out = set()
    if OUTREACH.exists():
        for line in OUTREACH.read_text(encoding="utf-8").splitlines():
            f = (line.split("\t") + [""] * 7)[:7]
            m = re.search(r"rows=([\d,]+)", f[6])
            if m and f[5] in ("sent", "followup") and re.match(r"\d{4}-\d{2}-\d{2}$", f[0]) \
                    and (today - date.fromisoformat(f[0])).days < days:
                out.update(int(x) for x in m.group(1).split(",") if x)
    return out


def log(company, vid, action, note):
    with open(OUTREACH, "a", encoding="utf-8") as fh:
        fh.write("\t".join([date.today().isoformat(), company, "чат hh", "hh-chat", vid, action, note]) + "\n")


def hh_chat(vid, send_file=None):
    cmd = ["node", str(HERE / "hh.mjs"), "chat", vid] + (["--send-file", send_file] if send_file else [])
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE, timeout=300)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"error": (p.stderr or p.stdout).strip()[-300:]}


def gate(company, title, text, timeout):
    """-> True — отправлять. «нет» — False; таймаут (exit 2) — True."""
    if not shutil.which("tg"):
        return False
    p = subprocess.run(["tg", "ask", f"Чат hh: {company} — {title}\n\n{text}", "--buttons", "отправить,нет",
                        "--timeout", str(timeout)], capture_output=True, text=True)
    if p.returncode == 2:
        return True
    return p.stdout.strip().splitlines()[-1:] == ["отправить"] if p.returncode == 0 else False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=5, help="рабочих дней со дня отклика (5)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--tg", action="store_true")
    ap.add_argument("--limit", type=int, default=8, help="не больше N чатов за прогон (8)")
    ap.add_argument("--timeout", type=int, default=900, help="секунд на ответ в гейте; молчание = отправить")
    a = ap.parse_args()

    rows = S.fetch_sheet()
    if rows is None:
        sys.exit("ERR: sheet.py не настроен")
    today, done, summary = date.today(), chatted(), []
    # открытые строки «Ожидание ответа» со ссылкой hh и достаточно старым откликом
    cand, recent = [], touched_rows(today)
    for r in rows:
        v = r["values"] + [""] * 14
        if v[4] != "Ожидание ответа" or r["row"] in recent:
            continue
        if (due := S.due_date(v[5], today)) and due > today:       # F «~30.12: ждать» — запланированное ожидание
            continue
        ids = re.findall(r"hh\.ru/vacancy/(\d+)", v[8] + " " + v[13])
        d = S.parse_c(v[2])
        if not ids or not d or workdays_since(d, today) < a.days or ids[0] in done:
            continue
        cand.append((r["row"], v, ids[0], d))
    import sheet
    for row, v, vid, d in cand[:a.limit]:
        company, title = v[0], v[1]
        tag = f"#{row} {company} — {title[:60]}"
        st = hh_chat(vid)
        if st.get("error") or not st.get("found"):
            summary.append(f"{tag}: не нашёл отклик на hh ({st.get('error', 'нет в списке откликов')[:80]})")
            continue
        if not st.get("open"):
            summary.append(f"{tag}: чат закрыт работодателем")
            if a.apply:
                log(company, vid, "closed", "чат закрыт")
                sheet.call("update", row_number=row, set={}, append={"N": f"{today:%d.%m} чат hh закрыт работодателем"})
            continue
        text = TEXT.format(date=d.strftime("%d.%m"), title=title)
        if not a.apply:
            summary.append(f"{tag}: чат открыт → написал бы")
            continue
        if not gate(company, title, text, a.timeout):
            summary.append(f"{tag}: не отправлено (нет в гейте)")
            log(company, vid, "skip", "отказ в гейте")
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
            fh.write(text)
        res = hh_chat(vid, fh.name)
        Path(fh.name).unlink(missing_ok=True)
        if not res.get("sent"):
            summary.append(f"{tag}: отправка не подтвердилась — проверь чат руками {res.get('chat', '')}")
            continue
        log(company, vid, "sent", f"rows={row} {title[:60]}")
        nxt = today + timedelta(days=7)
        sheet.call("update", row_number=row, set={"F": f"{nxt:%d.%m}: ответа в чате hh нет → autoclose закроет"},
                   append={"N": f"{today:%d.%m} написал в чат hh: «{text.splitlines()[0][:120]}»"})
        summary.append(f"{tag}: написал в чат")
    if not cand:
        summary.append("нечего: нет открытых hh-откликов старше порога без сообщения в чате")
    print("\n".join(summary))
    if a.tg and a.apply and any("написал" in s or "закрыт" in s or "проверь" in s for s in summary):
        subprocess.run(["tg", "send", "Чаты hh:\n" + "\n".join(summary)], timeout=60)


if __name__ == "__main__":
    main()
