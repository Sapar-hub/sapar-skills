#!/usr/bin/env python3
"""Одно письмо рекрутеру/тимлиду через SMTP Mail.ru + копия в «Отправленные» + запись в outreach.tsv.

  send.py --to addr --subject S --body-file body.txt [--attach resume.pdf] [--followup] --yes

Без --yes печатает письмо и выходит (черновик для показа пользователю).
Отказывает, если адресата нет в contacts.tsv, если ему уже писали меньше 7 дней назад,
если исчерпан лимит касаний (2) или отправлено ≥10 писем за сегодня.
Почта: MAILRU_USER, MAILRU_APPPASS (пароль для внешних приложений). Пароль не печатается.
"""
import argparse, base64, datetime as dt, email.utils, imaplib, mimetypes, os, smtplib, ssl, sys, time
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contacts as C  # noqa: E402

SMTP_HOST, IMAP_HOST = "smtp.mail.ru", "imap.mail.ru"
SENT_FOLDER = os.environ.get("JOB_CONTACTS_SENT", "Отправленные")
DAILY_LIMIT = 10


def imap_utf7(s):
    out, buf = [], ""
    for ch in s + "\x00":
        if 0x20 <= ord(ch) <= 0x7e or ch == "\x00":
            if buf:
                b = base64.b64encode(buf.encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
                out.append(f"&{b}-"); buf = ""
            if ch != "\x00":
                out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    return "".join(out)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--to", required=True); p.add_argument("--subject", required=True)
    p.add_argument("--body-file", required=True); p.add_argument("--attach", action="append", default=[])
    p.add_argument("--followup", action="store_true"); p.add_argument("--yes", action="store_true")
    a = p.parse_args()

    to = a.to.strip().lower()
    rows = [r for r in C.read_tsv(C.CONTACTS, C.C_FIELDS) if r["value"].lower() == to]
    if not rows:
        C.die(f"{to} нет в contacts.tsv — сначала contacts.py add с источником")
    r = rows[0]
    if r["status"] in ("bounced", "skip"):
        C.die(f"{to}: статус {r['status']}")
    why = C.check_touch(to, a.followup)
    if why:
        C.die(f"{to}: {why}")
    today_sent = [o for o in C.read_tsv(C.OUTREACH, C.O_FIELDS)
                  if o["date"] == dt.date.today().isoformat() and o["channel"] == "email"
                  and o["action"] in ("sent", "followup")]
    if len(today_sent) >= DAILY_LIMIT:
        C.die(f"сегодня уже {len(today_sent)} писем — лимит {DAILY_LIMIT}")

    user = os.environ.get("MAILRU_USER")
    body = Path(a.body_file).read_text(encoding="utf-8").strip() + "\n"
    msg = EmailMessage()
    msg["From"] = user or "MAILRU_USER"
    msg["To"] = to
    msg["Subject"] = a.subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=(user or "x@mail.ru").split("@")[-1])
    msg.set_content(body)
    for f in a.attach:
        fp = Path(f).expanduser()
        if not fp.exists():
            C.die(f"нет файла {fp}")
        ct = (mimetypes.guess_type(fp.name)[0] or "application/octet-stream").split("/")
        msg.add_attachment(fp.read_bytes(), maintype=ct[0], subtype=ct[1], filename=fp.name)

    print(f"Кому: {r['name']} ({r['role']}, {r['company']}) <{to}>\nТема: {a.subject}\n"
          f"Вложения: {', '.join(Path(f).name for f in a.attach) or '—'}\n\n{body}")
    if not a.yes:
        print("— черновик; отправка только с --yes после подтверждения пользователя")
        return
    pw = os.environ.get("MAILRU_APPPASS")
    if not (user and pw):
        C.die("нужны MAILRU_USER и MAILRU_APPPASS")
    with smtplib.SMTP_SSL(SMTP_HOST, 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
    try:
        with imaplib.IMAP4_SSL(IMAP_HOST) as m:
            m.login(user, pw)
            m.append(f'"{imap_utf7(SENT_FOLDER)}"', "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    except Exception as e:
        print(f"⚠ письмо ушло, но копия в «{SENT_FOLDER}» не легла: {e}", file=sys.stderr)
    C.cmd_log(argparse.Namespace(value=to, action="followup" if a.followup else "sent",
                                 note=f"email: {a.subject}", checked=True))
    print("ОТПРАВЛЕНО")


if __name__ == "__main__":
    main()
