#!/usr/bin/env python3
"""tg-bridge: Telegram-бот как канал «человек в цикле» для агентов.

  tg daemon                                  единственный long-poll getUpdates (systemd --user)
  tg send TEXT | --stdin [--html] [--photo F] [--doc F] [--button "label=action:arg"]... [--cols N]
  tg ask TEXT [--photo F] (--buttons "да,нет" | --text) [--timeout 900]
                                             ждёт ответа; stdout = ответ, exit 2 = таймаут
  tg edit MESSAGE_ID TEXT | --stdin [--html]  переписать своё сообщение (живой статус, без уведомления)
  tg inbox [--peek] [--count]                пересланное боту, JSON-строки с "untrusted": true
  tg action add NAME -- argv... {arg}        кнопка "label=NAME:arg" запускает argv без shell
  tg action list | tg action rm NAME
  tg whoami                                  проверка токена и привязанного чата
  tg progress --title T --log F [...]        живой статус прогона из stream-json (см. progress.py)

Задачи из чата: если есть ~/.config/tg-bridge/agent.json, обычный текст пользователя (не пересланный)
становится задачей агенту: scripts/agent.py запускает её и показывает ход в одном сообщении.

Конфиг: $TG_BRIDGE_CONFIG (~/.config/tg-bridge): env (TG_TOKEN, TG_PROXY), chat, actions.json
Данные: $TG_BRIDGE_DATA (~/.local/share/tg-bridge): state.json, asks/, answers/, btn/, inbox/
Привязка: первый /start в личке боту, пока файла chat нет. Всё из других чатов отбрасывается.
"""
import argparse, fcntl, json, mimetypes, os, secrets, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

CONF = Path(os.environ.get("TG_BRIDGE_CONFIG", "~/.config/tg-bridge")).expanduser()
DATA = Path(os.environ.get("TG_BRIDGE_DATA", "~/.local/share/tg-bridge")).expanduser()
MAX_TEXT, MAX_CAPTION, MAX_FILE = 4096, 1024, 20 * 1024 * 1024


def die(msg, code=1):
    print(f"ERR: {msg}", file=sys.stderr)
    sys.exit(code)


def d(*parts):
    p = DATA.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_env():
    env = {}
    f = CONF / "env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    for k in ("TG_TOKEN", "TG_PROXY"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def chat_id():
    f = CONF / "chat"
    return int(f.read_text().strip()) if f.exists() and f.read_text().strip() else None


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# --- Bot API ---

class Api:
    def __init__(self, env=None):
        env = env or load_env()
        if not env.get("TG_TOKEN"):
            die(f"нет TG_TOKEN в {CONF}/env")
        self.base = f"https://api.telegram.org/bot{env['TG_TOKEN']}/"
        self.file_base = f"https://api.telegram.org/file/bot{env['TG_TOKEN']}/"
        h = [urllib.request.ProxyHandler({"https": env["TG_PROXY"]})] if env.get("TG_PROXY") else []
        self.opener = urllib.request.build_opener(*h)

    def call(self, method, files=None, timeout=70, **params):
        params = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                  for k, v in params.items() if v is not None}
        if files:
            body, ctype = multipart(params, files)
        else:
            body, ctype = json.dumps(params, ensure_ascii=False).encode(), "application/json"
        req = urllib.request.Request(self.base + method, data=body, headers={"Content-Type": ctype})
        for attempt in range(3):
            try:
                with self.opener.open(req, timeout=timeout) as r:
                    res = json.loads(r.read())
            except urllib.error.HTTPError as e:
                res = json.loads(e.read() or b"{}")
            if res.get("ok"):
                return res["result"]
            retry = (res.get("parameters") or {}).get("retry_after")
            if retry and attempt < 2:                     # 429: ждём, сколько сказали
                time.sleep(retry + 1)
                continue
            raise RuntimeError(f"{method}: {res.get('description', res)}")

    def download(self, file_id, dest):
        info = self.call("getFile", file_id=file_id)
        with self.opener.open(self.file_base + info["file_path"], timeout=120) as r:
            dest.write_bytes(r.read())
        return dest


def multipart(params, files):
    boundary = "tgb" + secrets.token_hex(12)
    out = []
    for k, v in params.items():
        out += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode(), str(v).encode(), b"\r\n"]
    for k, path in files.items():
        p = Path(path)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        out += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{p.name}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n".encode(), p.read_bytes(), b"\r\n"]
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


# --- отправка ---

def keyboard(rows):
    return {"inline_keyboard": rows} if rows else None


def deliver(api, text, html=False, photo=None, doc=None, markup=None, reply_to=None):
    """Одно логическое сообщение. Клавиатура — на последнем куске. -> (message_id, kind)."""
    cid = chat_id() or die("чат не привязан: напиши боту /start при запущенном `tg daemon`")
    mode = "HTML" if html else None
    if len(text) > MAX_TEXT and not doc:                       # длинный текст — файлом
        f = d("out", f"msg-{int(time.time())}.txt")
        f.write_text(text, encoding="utf-8")
        doc, text, mode = f, text[:300].rsplit(" ", 1)[0] + " …(полностью в файле)", None
    media = ("sendPhoto", "photo", photo) if photo else ("sendDocument", "document", doc) if doc else None
    if media:
        cap_fits = len(text) <= MAX_CAPTION
        m = api.call(media[0], files={media[1]: media[2]}, chat_id=cid, reply_to_message_id=reply_to,
                     caption=text if cap_fits else None, parse_mode=mode if cap_fits else None,
                     reply_markup=markup if cap_fits else None)
        if cap_fits:
            return m["message_id"], "caption"
    m = api.call("sendMessage", chat_id=cid, text=text or "—", parse_mode=mode, reply_markup=markup,
                 link_preview_options={"is_disabled": True},
                 reply_parameters={"message_id": int(reply_to), "allow_sending_without_reply": True} if reply_to else None)
    return m["message_id"], "text"


def cmd_send(a):
    text = sys.stdin.read() if a.stdin else (a.text or "")
    if a.stdin and not text.strip() and not (a.photo or a.doc):
        return                                                # пустой stdin (нечего напоминать) — молчим
    rows = []
    for b in a.button:
        label, _, target = b.partition("=")
        name, _, arg = target.partition(":")
        if not (label and name):
            die(f"кнопка: «label=action:arg», а не «{b}»")
        actions = load_actions()
        if name not in actions:
            die(f"нет action «{name}»: tg action add {name} -- …")
        bid = secrets.token_hex(6)
        write_atomic(d("btn", f"{bid}.json"), json.dumps({"action": name, "arg": arg, "label": label}, ensure_ascii=False))
        rows.append([{"text": label, "callback_data": f"a:{bid}"}])
    flat = [r[0] for r in rows]
    cols = a.cols or (4 if all(len(b["text"]) <= 14 for b in flat) else 1)   # короткие кнопки — по 4 в ряд
    rows = [flat[i:i + cols] for i in range(0, len(flat), cols)]
    mid, _ = deliver(Api(), text, a.html, a.photo, a.doc, keyboard(rows), a.reply_to)
    print(mid)


# --- приём (общий для daemon и ask) ---

def pending_asks():
    out = {}
    for f in d("asks", "x").parent.glob("*.json"):
        try:
            out[f.stem] = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return out


def answer(qid, value):
    if (DATA / "answers" / qid).exists():
        return False
    write_atomic(d("answers", qid), value)
    return True


def load_actions():
    f = CONF / "actions.json"
    return json.loads(f.read_text()) if f.exists() else {}


def run_action(name, arg):
    argv = load_actions().get(name)
    if not argv:
        return 127, f"нет action «{name}»"
    argv = [x.replace("{arg}", arg) for x in argv]            # подстановка поэлементно: arg — один argv, без shell
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 126, str(e)


def append_line(api, m, line, markup=None):
    """Дописать строку в уже отправленное сообщение (текст или подпись), сохранив разметку через entities."""
    try:
        if "caption" in m or "photo" in m or "document" in m:
            api.call("editMessageCaption", chat_id=m["chat"]["id"], message_id=m["message_id"],
                     caption=((m.get("caption") or "") + "\n\n" + line)[:MAX_CAPTION],
                     caption_entities=m.get("caption_entities") or [], reply_markup=markup)
        else:
            api.call("editMessageText", chat_id=m["chat"]["id"], message_id=m["message_id"],
                     text=((m.get("text") or "") + "\n\n" + line)[:MAX_TEXT], entities=m.get("entities") or [],
                     reply_markup=markup, link_preview_options={"is_disabled": True})
    except Exception:
        pass


def save_inbox(api, msg):
    item = {"untrusted": True, "date": msg.get("date"), "message_id": msg["message_id"],
            "text": msg.get("text") or msg.get("caption") or ""}
    fo = msg.get("forward_origin")
    if fo:
        src = fo.get("sender_user") or fo.get("chat") or {}
        item["forward_origin"] = {k: v for k, v in {
            "type": fo.get("type"), "date": fo.get("date"),
            "username": src.get("username"), "id": src.get("id"),
            "name": " ".join(filter(None, [src.get("first_name"), src.get("last_name")])) or src.get("title")
                    or fo.get("sender_user_name") or fo.get("author_signature"),
        }.items() if v}
    links = [e.get("url") for e in (msg.get("entities") or []) + (msg.get("caption_entities") or []) if e.get("url")]
    if links:
        item["links"] = links
    files = []
    cands = []
    if msg.get("photo"):
        cands.append((msg["photo"][-1], f"photo-{msg['message_id']}.jpg"))
    if msg.get("document"):
        cands.append((msg["document"], f"{msg['message_id']}-{Path(msg['document'].get('file_name') or 'file').name}"))
    for f, name in cands:
        if (f.get("file_size") or 0) > MAX_FILE:
            files.append({"skipped": name, "reason": "больше 20 МБ, Bot API не отдаёт"})
            continue
        try:
            files.append(str(api.download(f["file_id"], d("inbox", "files", name))))
        except Exception as e:
            files.append({"skipped": name, "reason": str(e)})
    if files:
        item["files"] = files
    write_atomic(d("inbox", f"{time.time():.3f}-{msg['message_id']}.json"), json.dumps(item, ensure_ascii=False))
    return item


def route(api, upd):
    """Один update. Возвращает короткую строку для лога (для тестов тоже)."""
    me = chat_id()
    if "callback_query" in upd:
        cq = upd["callback_query"]
        cid = (cq.get("message") or {}).get("chat", {}).get("id")
        if me is None or cid != me:
            return "drop:foreign-callback"
        kind, _, rest = (cq.get("data") or "").partition(":")
        if kind == "q":
            qid, _, idx = rest.partition(":")
            ask = pending_asks().get(qid)
            if not ask:
                api.call("answerCallbackQuery", callback_query_id=cq["id"], text="уже неактуально")
                return "q:stale"
            value = ask["options"][int(idx)]
            answer(qid, value)
            api.call("answerCallbackQuery", callback_query_id=cq["id"], text=value)
            return f"q:{qid}={value}"
        if kind == "a":
            f = DATA / "btn" / f"{rest}.json"
            if not f.exists():
                api.call("answerCallbackQuery", callback_query_id=cq["id"], text="уже сделано")
                return "a:stale"
            btn = json.loads(f.read_text())
            code, out = run_action(btn["action"], btn["arg"])
            m = cq["message"]
            api.call("answerCallbackQuery", callback_query_id=cq["id"],
                     text=f"{btn['label']}: {'ок' if code == 0 else 'ошибка'}")
            if code == 0:
                f.unlink(missing_ok=True)                      # кнопка одноразовая
                # итог — строкой в том же сообщении, без нового: чат не растёт от каждого нажатия
                rows = [[b for b in r if b.get("callback_data") != cq["data"]]
                        for r in (m.get("reply_markup") or {}).get("inline_keyboard", [])]
                note = (out.strip().splitlines() or [""])[-1][:200]
                append_line(api, m, f"{btn['label']}: {note or 'ок'}", {"inline_keyboard": [r for r in rows if r]})
            else:
                api.call("sendMessage", chat_id=me, reply_to_message_id=m["message_id"],
                         text=f"{btn['action']} {btn['arg']}: ошибка, код {code}" + (f"\n{out[-800:]}" if out else ""))
            return f"a:{btn['action']}:{code}"
        return "drop:unknown-callback"

    msg = upd.get("message")
    if not msg:
        return "drop:other"
    cid, text = msg["chat"]["id"], msg.get("text") or ""
    if me is None:                                             # привязка: только /start в личке
        if text.startswith("/start") and msg["chat"].get("type") == "private":
            write_atomic(CONF / "chat", str(cid))
            os.chmod(CONF / "chat", 0o600)
            api.call("sendMessage", chat_id=cid, text="Привязано. Сюда будут приходить вопросы агента; "
                     "пересланное мне уходит в inbox.")
            return f"pair:{cid}"
        return "drop:unpaired"
    if cid != me:
        return "drop:foreign"

    asks = pending_asks()
    text_asks = {q: x for q, x in asks.items() if x["mode"] == "text" and not (DATA / "answers" / q).exists()}
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    target = next((q for q, x in asks.items() if x.get("message_id") == reply_to), None) if reply_to else None
    if target is None and len(text_asks) == 1 and text and not msg.get("forward_origin"):
        target = next(iter(text_asks))
    if target and text:                                        # реплай на вопрос с кнопками — тоже ответ (свободный)
        answer(target, text)
        react(api, me, msg["message_id"])
        return f"q:{target}={text}"
    if text.startswith("/"):
        return command(api, me, msg, text)
    agent = load_agent()
    if agent and text and not msg.get("forward_origin"):          # свой текст = задача агенту
        enqueue_task(api, me, msg, text)
        return "task"
    save_inbox(api, msg)                                          # пересланное, файлы, фото = данные
    react(api, me, msg["message_id"])
    return "inbox"


# --- команды бота и задачи агенту ---

HELP = """Пиши задачу обычным текстом: агент возьмёт её в работу, ход будет виден в одном сообщении.
Пересланное и файлы уходят в inbox как данные; перешли, потом напиши, что с этим сделать.
Следующее сообщение продолжает ту же сессию; /new начинает заново.

/status — что сейчас идёт и что в очереди
/stop — прервать текущий прогон
/new — новая сессия агента
/inbox — непрочитанное"""
COMMANDS = [("status", "что идёт и что в очереди"), ("stop", "прервать прогон"), ("new", "новая сессия агента"),
            ("inbox", "непрочитанное")]


def load_agent():
    f = CONF / "agent.json"
    return json.loads(f.read_text()) if f.exists() else None


def runner_pid():
    f = DATA / "agent.pid"
    try:
        pid = int(f.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def enqueue_task(api, me, msg, text):
    task = {"text": text, "message_id": msg["message_id"], "date": msg.get("date"), "created": time.time()}
    write_atomic(d("tasks", f"{time.time():.3f}.json"), json.dumps(task, ensure_ascii=False))
    react(api, me, msg["message_id"], "👀")
    if runner_pid():
        api.call("sendMessage", chat_id=me, text="В очереди, после текущего прогона.",
                 reply_parameters={"message_id": msg["message_id"]})
        return
    log = open(d("agent.log"), "a")
    subprocess.Popen([sys.executable, "-I", str(Path(__file__).with_name("agent.py"))],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)


def command(api, me, msg, text):
    name = text[1:].split()[0].split("@")[0]
    say = lambda t: api.call("sendMessage", chat_id=me, text=t, link_preview_options={"is_disabled": True})
    if name in ("start", "help"):
        say(HELP if load_agent() else "Пересылай сообщения и файлы — уйдут в inbox. /inbox — что там.")
    elif name == "inbox":
        say(inbox_summary())
    elif name == "new":
        (DATA / "agent-session").unlink(missing_ok=True)
        say("Новая сессия: следующая задача начнётся с чистого контекста.")
    elif name == "stop":
        pid = runner_pid()
        if pid:
            import signal
            os.killpg(pid, signal.SIGTERM)                       # раннер и claude в одной группе процессов
            say("Остановлено. Очередь задач сохранена: /status.")
        else:
            say("Сейчас ничего не идёт.")
    elif name == "status":
        tasks = sorted(d("tasks", "x").parent.glob("*.json"))
        cur = DATA / "agent-current.json"
        lines = []
        if runner_pid() and cur.exists():
            c = json.loads(cur.read_text())
            lines.append(f"Идёт ({int((time.time() - c['started']) // 60)} мин): {c['text'][:120]}")
        else:
            lines.append("Сейчас ничего не идёт.")
        if tasks:
            lines.append(f"\nВ очереди: {len(tasks)}")
            lines += [f"- {json.loads(t.read_text())['text'][:80]}" for t in tasks[:10]]
        lines.append("\nСессия: " + ("продолжается" if (DATA / "agent-session").exists() else "новая"))
        say("\n".join(lines))
    elif name in load_actions() and not any("{arg}" in x for x in load_actions()[name]):
        code, out = run_action(name, "")                          # /queue и т.п. — action без аргумента
        say((out or "пусто")[-3500:] if code == 0 else f"{name}: ошибка, код {code}\n{out[-800:]}")
    else:
        say(f"Нет команды /{name}. /help")
    return f"cmd:{name}"


def inbox_summary(limit=15):
    """Для /inbox в боте: что лежит непрочитанным (агент ещё не забрал), по строке на элемент."""
    files = sorted(d("inbox", "x").parent.glob("*.json"))
    if not files:
        return "Inbox пуст."
    lines = [f"Inbox, непрочитано: {len(files)}", ""]
    for n, f in enumerate(files[-limit:], 1):
        try:
            it = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        fo = it.get("forward_origin") or {}
        who = " ".join(filter(None, [fo.get("name"), f"@{fo['username']}" if fo.get("username") else None])) or "от тебя"
        when = time.strftime("%d.%m %H:%M", time.localtime(it.get("date") or 0))
        body = " ".join((it.get("text") or "").split())[:80] or "(без текста)"
        extra = f", файлов: {len(it['files'])}" if it.get("files") else ""
        lines.append(f"{n}. {who}, {when}{extra}\n   {body}")
    if len(files) > limit:
        lines.append(f"\nи ещё {len(files) - limit} раньше")
    lines.append("\nЗабрать в работу: в сессии агента «что я скинул боту».")
    return "\n".join(lines)


def react(api, chat, message_id, emoji="👌"):
    """Реакция на сообщение пользователя: «принято», без лишнего текста в чате."""
    try:
        api.call("setMessageReaction", chat_id=chat, message_id=message_id,
                 reaction=[{"type": "emoji", "emoji": emoji}])
    except Exception:
        pass


class PollLock:
    def __init__(self):
        self.f = open(d("poll.lock"), "w")

    def acquire(self, block):
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
            return True
        except BlockingIOError:
            return False

    def release(self):
        fcntl.flock(self.f, fcntl.LOCK_UN)


def poll_once(api, timeout):
    st = d("state.json")
    state = json.loads(st.read_text()) if st.exists() else {}
    ups = api.call("getUpdates", offset=state.get("offset"), timeout=timeout,
                   allowed_updates=["message", "callback_query"])
    for u in ups:
        try:
            r = route(api, u)
        except Exception as e:
            r = f"error:{e}"
        print(f"{time.strftime('%H:%M:%S')} {u['update_id']} {r}", flush=True)
        state["offset"] = u["update_id"] + 1
        write_atomic(st, json.dumps(state))


def cmd_daemon(a):
    api, lock = Api(), PollLock()
    lock.acquire(block=True)
    print(f"tg-bridge daemon: chat={chat_id() or 'не привязан, жду /start'}", flush=True)
    try:                                                          # меню команд в клиенте Telegram
        extra = [(k, "action") for k, v in load_actions().items() if not any("{arg}" in x for x in v)]
        cmds = (COMMANDS if load_agent() else [("inbox", "непрочитанное")]) + extra
        api.call("setMyCommands", commands=[{"command": c, "description": t} for c, t in cmds])
    except Exception as e:
        print(f"WARN: setMyCommands: {e}", file=sys.stderr, flush=True)
    delay = 1
    while True:
        try:
            poll_once(api, 50)
            delay = 1
        except Exception as e:
            print(f"WARN: {e}", file=sys.stderr, flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def cmd_edit(a):
    """Переписать своё сообщение целиком (живой статус). Ошибку «message is not modified» глотаем."""
    text = sys.stdin.read() if a.stdin else (a.text or "")
    try:
        Api().call("editMessageText", chat_id=chat_id(), message_id=a.message_id, text=text[:MAX_TEXT] or "—",
                   parse_mode="HTML" if a.html else None, link_preview_options={"is_disabled": True})
    except RuntimeError as e:
        if "not modified" not in str(e):
            die(str(e))


# --- ask ---

def cmd_ask(a):
    if not a.text_mode and not a.buttons:
        die("нужно --buttons «a,b» или --text")
    api = Api()
    qid = secrets.token_hex(4)
    options = [o.strip() for o in a.buttons.split(",") if o.strip()] if a.buttons else []
    if a.text_mode:
        markup = {"force_reply": True, "input_field_placeholder": "ответ"[:64]}
    else:
        rows = [{"text": o, "callback_data": f"q:{qid}:{i}"} for i, o in enumerate(options)]
        markup = keyboard([rows] if sum(len(o) for o in options) <= 30 else [[r] for r in rows])
    ask = {"mode": "text" if a.text_mode else "buttons", "options": options, "created": time.time()}
    write_atomic(d("asks", f"{qid}.json"), json.dumps(ask, ensure_ascii=False))   # до отправки: ответ может прийти сразу
    text = (a.question or "") + ("\n\n(ответь реплаем)" if a.text_mode else "")
    mid, kind = deliver(api, text, a.html, a.photo, None, markup)
    ask.update(message_id=mid, kind=kind)
    write_atomic(d("asks", f"{qid}.json"), json.dumps(ask, ensure_ascii=False))

    lock, deadline, ans = PollLock(), time.time() + a.timeout, DATA / "answers" / qid
    try:
        while time.time() < deadline and not ans.exists():
            if lock.acquire(block=False):                      # демона нет — поллим сами, тем же роутером
                try:
                    poll_once(api, min(10, max(1, int(deadline - time.time()))))
                except Exception as e:
                    print(f"WARN: {e}", file=sys.stderr)
                    time.sleep(2)
                finally:
                    lock.release()
            else:
                time.sleep(1)
        value = ans.read_text(encoding="utf-8") if ans.exists() else None
    finally:
        (DATA / "asks" / f"{qid}.json").unlink(missing_ok=True)
        ans.unlink(missing_ok=True)
    mark = f"\n\nОтвет: {value}" if value is not None else "\n\nБез ответа (таймаут)"
    try:
        if kind == "caption":
            api.call("editMessageCaption", chat_id=chat_id(), message_id=mid, caption=(text + mark)[:MAX_CAPTION])
        else:
            api.call("editMessageText", chat_id=chat_id(), message_id=mid, text=(text + mark)[:MAX_TEXT],
                     link_preview_options={"is_disabled": True})
    except Exception:
        pass
    if value is None:
        sys.exit(2)
    print(value)


# --- inbox / actions / whoami ---

def cmd_inbox(a):
    files = sorted(d("inbox", "x").parent.glob("*.json"))
    if a.count:
        print(len(files))
        return
    for f in files:
        print(f.read_text(encoding="utf-8").strip())
        if not a.peek:
            f.replace(d("inbox", "done", f.name))


def cmd_action(a):
    acts = load_actions()
    if a.op == "list":
        for k, v in acts.items():
            print(f"{k}\t{json.dumps(v, ensure_ascii=False)}")
        return
    if not a.name:
        die("нужно имя action")
    if a.op == "rm":
        acts.pop(a.name, None)
    else:
        argv = a.argv[1:] if a.argv and a.argv[0] == "--" else a.argv
        if not argv:
            die("tg action add NAME -- argv... ({arg} — аргумент кнопки)")
        if not Path(argv[0]).is_absolute():
            die("argv[0] — абсолютный путь (демон под systemd не видит твой PATH)")
        acts[a.name] = argv
    CONF.mkdir(parents=True, exist_ok=True)
    write_atomic(CONF / "actions.json", json.dumps(acts, ensure_ascii=False, indent=1))
    print(f"OK: {a.op} {a.name}")


def cmd_whoami(a):
    me = Api().call("getMe")
    print(f"@{me['username']}  chat={chat_id() or 'не привязан'}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "progress":           # tg progress … → progress.py (живой статус)
        import runpy
        sys.argv = [str(Path(__file__).with_name("progress.py"))] + sys.argv[2:]
        runpy.run_path(sys.argv[0], run_name="__main__")
        return
    ap = argparse.ArgumentParser(prog="tg", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("daemon").set_defaults(f=cmd_daemon)
    sp.add_parser("whoami").set_defaults(f=cmd_whoami)
    s = sp.add_parser("send")
    s.add_argument("text", nargs="?")
    s.add_argument("--stdin", action="store_true")
    s.add_argument("--html", action="store_true")
    s.add_argument("--photo"); s.add_argument("--doc")
    s.add_argument("--button", action="append", default=[])
    s.add_argument("--cols", type=int, default=0, help="кнопок в ряду (по умолчанию: 4 коротких или 1 длинная)")
    s.add_argument("--reply-to", help="ответом на это сообщение")
    s.set_defaults(f=cmd_send)
    e = sp.add_parser("edit")
    e.add_argument("message_id", type=int); e.add_argument("text", nargs="?")
    e.add_argument("--stdin", action="store_true"); e.add_argument("--html", action="store_true")
    e.set_defaults(f=cmd_edit)
    q = sp.add_parser("ask")
    q.add_argument("question")
    q.add_argument("--buttons")
    q.add_argument("--text", dest="text_mode", action="store_true")
    q.add_argument("--html", action="store_true")
    q.add_argument("--photo")
    q.add_argument("--timeout", type=int, default=900)
    q.set_defaults(f=cmd_ask)
    i = sp.add_parser("inbox")
    i.add_argument("--peek", action="store_true"); i.add_argument("--count", action="store_true")
    i.set_defaults(f=cmd_inbox)
    ac = sp.add_parser("action")
    ac.add_argument("op", choices=["add", "list", "rm"]); ac.add_argument("name", nargs="?")
    ac.add_argument("argv", nargs=argparse.REMAINDER)
    ac.set_defaults(f=cmd_action)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
