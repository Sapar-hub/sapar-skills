#!/usr/bin/env python3
"""Почта Mail.ru по IMAP IDLE вместо опроса по таймеру: новое письмо → сразу разбор.

  mailwatch.py            слушать (systemd: job-apply-mailwatch.service, Restart=always)
  mailwatch.py --once     один цикл без IDLE: проверить папки и запустить нужное (для отладки)

По одному соединению на папку (IDLE работает только в выбранной папке). Сервер сообщает EXISTS —
читаем заголовки From новых писем (только чтение, флаг \\Seen не трогаем) и через DEBOUNCE секунд тишины:
  письмо от hh.ru (в любой папке, включая «Спам» — Mail.ru кладёт туда noreply@hh.ru) → hhstatus.py --apply --tg
  любое письмо в рабочих папках                                             → mailstatus.py --apply --tg
Остальной спам игнорируется. IDLE обновляется каждые RENEW секунд (сервер рвёт его через ~30 мин).
Креды: MAILRU_USER, MAILRU_APPPASS (worker.env), как у mailstatus.py.
"""
import argparse, email.header, imaplib, os, queue, re, subprocess, sys, threading, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST = "imap.mail.ru"
RENEW = 20 * 60
DEBOUNCE = 30
WORK = {"Отклики": "&BB4EQgQ6BDsEOAQ6BDg-", "INBOX": "INBOX", "Newsletters": "INBOX/Newsletters"}
SPAM = {"Спам": "&BCEEPwQwBDw-"}
HH = re.compile(r"@(?:[\w.-]+\.)?hh\.ru\b|headhunter", re.I)

events = queue.Queue()          # ("hh" | "mail", папка, от кого)


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def connect(raw):
    M = imaplib.IMAP4_SSL(HOST, timeout=60)
    M.login(os.environ["MAILRU_USER"], os.environ["MAILRU_APPPASS"])
    M.select(f'"{raw}"', readonly=True)
    return M


def max_uid(M):
    typ, d = M.uid("SEARCH", None, "ALL")
    ids = d[0].split() if typ == "OK" and d and d[0] else []
    return int(ids[-1]) if ids else 0


def senders(M, after):
    """UID > after → [(uid, from)]; BODY.PEEK — без пометки прочитанным."""
    typ, d = M.uid("SEARCH", None, f"UID {after + 1}:*")
    uids = [int(x) for x in (d[0].split() if typ == "OK" and d and d[0] else []) if int(x) > after]
    out = []
    for u in uids:
        typ, m = M.uid("FETCH", str(u), "(BODY.PEEK[HEADER.FIELDS (FROM)])")
        raw = next((p[1] for p in m if isinstance(p, tuple)), b"")
        frm = str(email.header.make_header(email.header.decode_header(raw.decode("utf-8", "replace").partition(":")[2].strip())))
        out.append((u, frm))
    return out


def watch(name, raw, spam):
    last, backoff = None, 5
    while True:
        try:
            M = connect(raw)
            top = max_uid(M)
            if last is None:
                last = top                                   # при старте — только новые с этого момента
            elif top > last:                                 # пропущенное, пока были отключены
                last = handle(M, name, last, spam)
            backoff = 5
            log(f"{name}: слушаю (uid {last})")
            started = time.monotonic()
            while time.monotonic() - started < RENEW:
                with M.idle(duration=min(RENEW, 29 * 60)) as idler:
                    got = any(typ == "EXISTS" for typ, _ in idler)   # блокируется до события или duration
                if got:
                    last = handle(M, name, last, spam)
                else:
                    break                                    # duration вышел без событий — переподключиться
            M.logout()
        except Exception as e:                                # обрыв сети/прокси, таймаут — переподключение
            log(f"{name}: {type(e).__name__}: {e} — переподключаюсь через {backoff} с")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


def handle(M, name, last, spam):
    new = senders(M, last)
    for uid, frm in new:
        is_hh = bool(HH.search(frm))
        if spam and not is_hh:
            continue
        events.put(("hh" if is_hh else "mail", name, frm))
        log(f"{name}: новое письмо от {frm[:60]}")
    return max([last] + [u for u, _ in new])


def run(script):
    log(f"→ {script} --apply --tg")
    if os.environ.get("MAILWATCH_DRY"):                     # отладка: только лог, без прогонов
        return
    r = subprocess.run([sys.executable, "-I", str(HERE / script), "--apply", "--tg"], cwd=HERE,
                       capture_output=True, text=True, timeout=900)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    log(f"  {script}: код {r.returncode}; " + " | ".join(tail))


def dispatcher():
    while True:
        kinds = {events.get()[0]}
        deadline = time.monotonic() + DEBOUNCE             # пачка писем → один прогон
        while (left := deadline - time.monotonic()) > 0:
            try:
                kinds.add(events.get(timeout=left)[0])
            except queue.Empty:
                break
        try:
            run("mailstatus.py")                             # любое новое письмо (и от hh — там бывают отказы текстом)
            if "hh" in kinds:
                run("hhstatus.py")
        except Exception as e:
            log(f"ERR прогона: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    if not (os.environ.get("MAILRU_USER") and os.environ.get("MAILRU_APPPASS")):
        sys.exit("нет MAILRU_USER / MAILRU_APPPASS")
    if a.once:
        run("mailstatus.py")
        run("hhstatus.py")
        return
    for name, raw in WORK.items():
        threading.Thread(target=watch, args=(name, raw, False), daemon=True).start()
    for name, raw in SPAM.items():
        threading.Thread(target=watch, args=(name, raw, True), daemon=True).start()
    dispatcher()


if __name__ == "__main__":
    main()
