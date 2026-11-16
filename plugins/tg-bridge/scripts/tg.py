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
  tg topic NAME [--cwd DIR] [--kind K]       новая вкладка (Threaded Mode), печатает id для TG_THREAD
  tg progress --title T --log F [...]        живой статус прогона из stream-json (см. progress.py)

Задачи из чата: если есть ~/.config/tg-bridge/agent.json, обычный текст пользователя (не пересланный)
становится задачей агенту: scripts/agent.py запускает её и показывает ход в одном сообщении.

Конфиг: $TG_BRIDGE_CONFIG (~/.config/tg-bridge): env (TG_TOKEN, TG_PROXY), chat, actions.json
Данные: $TG_BRIDGE_DATA (~/.local/share/tg-bridge): state.json, asks/, answers/, btn/, inbox/
Привязка: первый /start в личке боту, пока файла chat нет. Всё из других чатов отбрасывается.
"""
import argparse, fcntl, json, mimetypes, os, re, secrets, shutil, subprocess, sys, time, urllib.error, urllib.request
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

def links_as_names(method, params):
    """Всё, что бот пишет человеку без своей разметки, уходит HTML-ем: [текст](url) и голые ссылки — кликабельные,
    без простыни адреса. Одна точка для send/ask/edit/ответов на команды/подписей."""
    if method not in ("sendMessage", "editMessageText", "sendPhoto", "sendDocument", "editMessageCaption"):
        return params
    key = "text" if "text" in params else "caption" if "caption" in params else None
    if key and params.get(key) and not params.get("parse_mode") and URL_RE.search(params[key]):
        html_text = linkify(params[key])
        if len(html_text) <= (MAX_CAPTION if key == "caption" else MAX_TEXT):   # не влезло — шлём как было
            params = {**params, key: html_text, "parse_mode": "HTML"}
    return params


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
        params = links_as_names(method, params)
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

def env_thread():
    """Вкладка (топик) текущего прогона: агент и worker получают её через TG_THREAD — send/ask/edit/inbox идут туда."""
    v = os.environ.get("TG_THREAD", "").strip()
    return int(v) if v.isdigit() else None


def threads():
    f = DATA / "threads.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save_thread(tid, **info):
    t = threads()
    t[str(tid)] = {**t.get(str(tid), {}), **info}
    write_atomic(d("threads.json"), json.dumps(t, ensure_ascii=False, indent=1))


def create_topic(api, name, **info):
    """Новая вкладка в личке с ботом (Threaded Mode). -> message_thread_id"""
    name = " ".join(name.split())[:120] or "задача"
    r = api.call("createForumTopic", chat_id=chat_id(), name=name)
    save_thread(r["message_thread_id"], name=name, created=time.time(), **info)
    return r["message_thread_id"]


CONTROL_NAME = "Пульт"


def control_thread(api):
    """Вкладка «Пульт» (kind=control): всё, что шлётся без вкладки — кнопки дайджестов, уведомления, ответы
    на команды из неё. Общий чат в Threaded Mode показывает переписку всех вкладок разом, туда не пишем.
    Нет — создаём; не вышло — None (тогда общий чат)."""
    for tid, t in threads().items():
        if t.get("kind") == "control":
            return int(tid)
    try:
        return create_topic(api, CONTROL_NAME, kind="control")
    except Exception as e:
        print(f"WARN: вкладка «Пульт» не создана: {e}", file=sys.stderr)
        return None


def forget_control():
    t = threads()
    for tid in [k for k, v in t.items() if v.get("kind") == "control"]:
        t.pop(tid)
    write_atomic(d("threads.json"), json.dumps(t, ensure_ascii=False, indent=1))


def keyboard(rows):
    return {"inline_keyboard": rows} if rows else None


def deliver(api, text, html=False, photo=None, doc=None, markup=None, reply_to=None, thread=None):
    """Одно логическое сообщение. Клавиатура — на последнем куске. -> (message_id, kind).
    thread: вкладка; по умолчанию TG_THREAD, без неё — «Пульт». Вкладку удалили — «Пульт» (пересоздаётся)."""
    thread = thread if thread is not None else env_thread()
    control = thread is None
    if control:
        thread = control_thread(api)
    try:
        return _deliver(api, text, html, photo, doc, markup, reply_to, thread)
    except RuntimeError as e:
        if not (thread and "thread not found" in str(e).lower()):
            raise
        if control:                                            # «Пульт» удалили руками — создать заново
            forget_control()
        return _deliver(api, text, html, photo, doc, markup, None, control_thread(api))


URL_RE = re.compile(r"\[([^\]\n]{1,80})\]\((https?://[^\s)]+)\)|(https?://[^\s<>()\[\]«»\"']+[^\s<>()\[\]«»\"'.,;:!?])")


def linkify(text):
    """Обычный текст -> HTML: [текст](url) и голые ссылки — кликабельные, без простыни адреса
    (голая ссылка показывается именем сайта). Остальное экранируется."""
    from urllib.parse import urlparse
    out, pos = [], 0
    for m in URL_RE.finditer(text):
        out.append(html_escape(text[pos:m.start()]))
        label, url = (m.group(1), m.group(2)) if m.group(2) else (None, m.group(3))
        if label is None:
            label = (urlparse(url).hostname or url).removeprefix("www.")
        out.append(f'<a href="{html_escape(url, quote=True)}">{html_escape(label)}</a>')
        pos = m.end()
    out.append(html_escape(text[pos:]))
    return "".join(out)


def html_escape(s, quote=False):
    import html as _h
    return _h.escape(s, quote=quote)


def _deliver(api, text, html, photo, doc, markup, reply_to, thread):
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
                     message_thread_id=thread,
                     caption=text if cap_fits else None, parse_mode=mode if cap_fits else None,
                     reply_markup=markup if cap_fits else None)
        if cap_fits:
            return m["message_id"], "caption"
    m = api.call("sendMessage", chat_id=cid, text=text or "—", parse_mode=mode, reply_markup=markup,
                 message_thread_id=thread,
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


CMD_REPLY_TTL = 1800


def pending_command(reply_to):
    """Имя action, если reply_to — свежая подсказка команды; старые подсказки удаляются."""
    if not reply_to:
        return None
    f = DATA / "cmdreply" / f"{reply_to}.json"
    try:
        j = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - j.get("created", 0) > CMD_REPLY_TTL:
        f.unlink(missing_ok=True)
        return None
    return j.get("action")


def load_command_meta():
    """commands.json: {action: {"desc": для меню и /help, "hint": серая подсказка в поле ввода}}.
    Старый вид {action: "desc"} тоже читается."""
    f = CONF / "commands.json"
    try:
        raw = json.loads(f.read_text()) if f.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: (v if isinstance(v, dict) else {"desc": v}) for k, v in raw.items()}


def load_command_desc():
    return {k: v.get("desc", "") for k, v in load_command_meta().items() if v.get("desc")}


def run_action(name, arg, thread=None):
    """thread: вкладка, откуда пришла команда/кнопка — идёт в TG_THREAD, и `tg send` из action отвечает туда же,
    а не в общий чат."""
    argv = load_actions().get(name)
    if not argv:
        return 127, f"нет action «{name}»"
    argv = [x.replace("{arg}", arg) for x in argv]            # подстановка поэлементно: arg — один argv, без shell
    env = {**os.environ, "TG_THREAD": str(thread) if thread else ""}
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL, env=env)
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
            "thread": msg_thread(msg), "text": msg.get("text") or msg.get("caption") or ""}
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


def msg_thread(msg):
    return msg.get("message_thread_id") if msg.get("is_topic_message") else None


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
            m = cq["message"]
            code, out = run_action(btn["action"], btn["arg"], thread=msg_thread(m))
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
                         message_thread_id=m.get("message_thread_id") if m.get("is_topic_message") else None,
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

    thread = msg_thread(msg)
    asks = pending_asks()
    text_asks = {q: x for q, x in asks.items() if x["mode"] == "text" and x.get("thread") == thread
                 and not (DATA / "answers" / q).exists()}
    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
    target = next((q for q, x in asks.items() if x.get("message_id") == reply_to), None) if reply_to else None
    pending_name = pending_command(reply_to)                   # реплай на подсказку команды (её аргумент)
    to_cmd = bool(pending_name)
    if target is None and len(text_asks) == 1 and text and not msg.get("forward_origin") and not to_cmd:
        target = next(iter(text_asks))
    if target and text:                                        # реплай на вопрос с кнопками — тоже ответ (свободный)
        answer(target, text)
        react(api, me, msg["message_id"])
        return f"q:{target}={text}"
    if pending_name and text and not text.startswith("/"):
        # реплай на подсказку команды («/contact» без текста) = её аргумент; подсказка живёт CMD_REPLY_TTL,
        # так что можно ответить на неё несколько раз (несколько контактов подряд)
        name = pending_name
        if name in load_actions():
            code, out = run_action(name, text, thread=thread)
            api.call("sendMessage", chat_id=me, text=(out or "пусто")[-3500:] if code == 0
                     else f"{name}: ошибка, код {code}\n{out[-800:]}", message_thread_id=thread,
                     reply_parameters={"message_id": msg["message_id"]}, link_preview_options={"is_disabled": True})
            return f"cmd:{name}+reply"
    if text.startswith("/"):
        return command(api, me, msg, text)
    if "forum_topic_created" in msg and msg.get("message_thread_id"):
        FRESH[msg["message_thread_id"]] = time.time()              # клиент сам завёл вкладку под сообщение из «All»
    if any(k.startswith(("forum_topic_", "general_forum_topic_")) for k in msg) or msg.get("pinned_message"):
        return "drop:service"                                     # «тема создана» и пр. — не пересылка, без фантомной сессии
    agent = load_agent()
    if agent and text and not msg.get("forward_origin"):          # свой текст = задача агенту
        enqueue_task(api, me, msg, text, thread)                  # в общем чате — новая вкладка, во вкладке — её сессия
        return "task"
    save_inbox(api, msg)                                          # пересланное, файлы, фото = данные
    react(api, me, msg["message_id"])
    if agent:                                                     # …но и повод продолжить: агент сам заберёт inbox
        enqueue_forward(msg, thread)
        return "inbox+task"
    return "inbox"


# --- команды бота и задачи агенту ---

HELP = """Одна вкладка = одна задача со своим контекстом.

Написал в общем чате — бот откроет новую вкладку и начнёт там с чистого листа.
Написал или переслал внутри вкладки — продолжение этой задачи.
Пересланное и файлы — данные: перешли, потом напиши, что с этим сделать (или ничего: через 15 с агент продолжит сам).
Каждый автоотклик тоже идёт в своей вкладке; в ней же можно продолжить разговор.

/status — что идёт и что в очереди
/stop — прервать текущий прогон
/inbox — непрочитанное"""
COMMANDS = [("help", "все команды с примерами"), ("new", "задача в отдельной вкладке: /new текст"), ("reset", "забыть контекст этого чата/вкладки"), ("status", "что идёт и что в очереди"), ("stop", "прервать прогон"),
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


FORWARD_WAIT = 15            # с: переслал несколько сообщений и дописал текст — это одна задача


def running():
    out = []
    for f in d("running", "x").parent.glob("*.json"):
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
    return out


def start_runner():
    if runner_pid():
        return False
    log = open(d("agent.log"), "a")
    subprocess.Popen([sys.executable, "-I", str(Path(__file__).with_name("agent.py"))],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    return True


def enqueue_forward(msg, thread=None):
    """Пересылка без текста — задача «продолжи», с задержкой: текст следом её заменит (agent.py склеивает)."""
    f = d("tasks", f"forward-{thread or 'general'}.json")         # одна на пачку пересылок во вкладке
    task = json.loads(f.read_text()) if f.exists() else {"kind": "forward", "message_id": msg["message_id"],
                                                          "thread": thread, "created": time.time(), "count": 0}
    task.update(not_before=time.time() + FORWARD_WAIT, count=task["count"] + 1, last_message_id=msg["message_id"])
    write_atomic(f, json.dumps(task, ensure_ascii=False))
    start_runner()


def enqueue_task(api, me, msg, text, thread=None, new_tab=False):
    task = {"text": text, "message_id": msg["message_id"], "thread": thread, "date": msg.get("date"),
            "created": time.time(), **({"new_tab": True} if new_tab else {})}
    write_atomic(d("tasks", f"{time.time():.6f}-{msg['message_id']}.json"), json.dumps(task, ensure_ascii=False))
    react(api, me, msg["message_id"], "👀")
    run = running()
    limit = int((load_agent() or {}).get("parallel", 3))
    busy = thread is not None and any(r.get("thread") == thread for r in run)
    if not start_runner() and (busy or len(run) >= limit):
        why = "в этой вкладке ещё идёт прошлое" if busy else f"уже идут {len(run)} задачи"
        api.call("sendMessage", chat_id=me, text=f"В очереди: {why}.", message_thread_id=thread,
                 reply_parameters={"message_id": msg["message_id"]})


# Вкладки, которые клиент Telegram создал сам: в Threaded Mode сообщение, отправленное из общего списка «All»,
# становится новой вкладкой (сначала приходит служебное forum_topic_created, следом — само сообщение).
FRESH = {}
FRESH_TTL = 120


def auto_topic(api, msg):
    """Команда пришла во вкладку, которую клиент только что завёл сам (не наша, не из threads.json)? -> её id."""
    here = msg_thread(msg)
    born = FRESH.get(here) if here else None
    if born and time.time() - born < FRESH_TTL and str(here) not in threads():
        return here
    return None


def command(api, me, msg, text):
    # команда из «All»: отвечаем в «Пульт», а пустую автовкладку убираем — не плодим вкладку на каждую команду
    stray = auto_topic(api, msg)
    if stray:
        ctl = control_thread(api)
        if ctl:
            msg = {**msg, "is_topic_message": True, "message_thread_id": ctl}
        try:
            return command_in(api, me, msg, text)
        finally:
            FRESH.pop(stray, None)
            try:
                api.call("deleteForumTopic", chat_id=me, message_thread_id=stray)
            except Exception as e:
                print(f"WARN: автовкладка {stray} не удалена: {e}", file=sys.stderr, flush=True)
    return command_in(api, me, msg, text)


def command_in(api, me, msg, text):
    name = text[1:].split()[0].split("@")[0]
    say = lambda t: api.call("sendMessage", chat_id=me, text=t, message_thread_id=msg_thread(msg),
                             link_preview_options={"is_disabled": True})
    if name in ("start", "help"):
        head = HELP if load_agent() else "Пересылай сообщения и файлы — уйдут в inbox. /inbox — что там."
        meta, acts = load_command_meta(), load_actions()
        extra = [f"/{k} — {meta[k]['desc']}" + (f"\n    пример: {meta[k]['hint']}" if meta[k].get("hint") else "")
                 for k in acts if k in meta and meta[k].get("desc") and re.fullmatch(r"[a-z0-9_]{1,32}", k)]
        say(head + ("\n\nЕщё команды:\n" + "\n".join(extra) if extra else "")
            + "\n\nКоманда с аргументами без текста (например /contact) пришлёт пример — ответь на него реплаем.")
    elif name == "inbox":
        say(inbox_summary())
    elif name == "reset":
        here = msg_thread(msg)
        f = DATA / "sessions" / str(here or "general")
        existed = f.exists()
        f.unlink(missing_ok=True) if f.is_file() else shutil.rmtree(f, ignore_errors=True)
        say(("Контекст сброшен: следующее сообщение начнёт новую сессию " + ("этой вкладки." if here else "общего чата."))
            if existed else "Здесь сессии ещё не было.")
    elif name == "new":
        rest = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
        if not rest:
            say("/new <задача> — отдельная вкладка под задачу. Без /new сообщения в общем чате идут в одну общую "
                "сессию (agent.json \"general\": \"single\"), а внутри вкладки — в её сессию.")
        else:
            enqueue_task(api, me, msg, rest, thread=None, new_tab=True)
    elif name == "stop":
        import signal
        here = msg_thread(msg)
        victims = [r for r in running() if here is None or r.get("thread") == here]   # во вкладке — только её задачу
        for r in victims:
            try:
                os.killpg(r["pgid"], signal.SIGTERM)
            except (OSError, KeyError):
                pass
        say(f"Остановлено: {len(victims)}. Очередь сохранена: /status." if victims else "Здесь ничего не идёт.")
    elif name == "status":
        tasks = sorted(d("tasks", "x").parent.glob("*.json"))
        run = running() if runner_pid() else []
        lines = [f"Идёт: {len(run)}" if run else "Сейчас ничего не идёт."]
        for c in run:
            tab = threads().get(str(c.get("thread")), {}).get("name")
            lines.append(f"- {int((time.time() - c['started']) // 60)} мин, «{tab or c.get('title', '')}»")
        owner = Path.home() / ".local/share/job-apply/browser.owner"
        if owner.exists():
            lines.append(f"Браузер занят: {owner.read_text().strip()}")
        if tasks:
            lines.append(f"\nВ очереди: {len(tasks)}")
            lines += [f"- {json.loads(t.read_text()).get('text') or 'пересланное'}"[:82] for t in tasks[:10]]
        say("\n".join(lines))
    elif name in load_actions():
        # /queue — action без аргумента; /contact Acme; Анна; @x — текст после команды целиком идёт в {arg}
        # (одним элементом argv, без shell). Пустой текст у команды с {arg} -> "help": скрипт печатает формат.
        rest = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
        takes_arg = any("{arg}" in x for x in load_actions()[name])
        if takes_arg and not rest:
            # без текста: формат от самого скрипта + поле ввода с подсказкой; реплай на это сообщение = аргумент
            code, out = run_action(name, "help", thread=msg_thread(msg))
            hint = load_command_meta().get(name, {}).get("hint", "")
            m = api.call("sendMessage", chat_id=me, message_thread_id=msg_thread(msg),
                         text=((out or f"/{name}: напиши аргумент") + "\n\nОтветь на это сообщение — уйдёт в /" + name + " (реплаями 30 мин, можно несколько)")[-3500:],
                         reply_markup={"force_reply": True, "input_field_placeholder": (hint or "аргумент")[:64]},
                         link_preview_options={"is_disabled": True})
            if isinstance(m, dict) and m.get("message_id"):
                write_atomic(d("cmdreply", f"{m['message_id']}.json"), json.dumps({"action": name, "created": time.time()}))
            return f"cmd:{name}?"
        code, out = run_action(name, rest if takes_arg else "", thread=msg_thread(msg))
        if code or (out or "").strip():                         # пустой вывод: команда ответила сама (tg send)
            say(out[-3500:] if code == 0 else f"{name}: ошибка, код {code}\n{out[-800:]}")
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
        desc = load_command_desc()                                # action с описанием (или без {arg}) = команда в меню
        extra = [(k, desc.get(k, "action")) for k, v in load_actions().items()
                 if re.fullmatch(r"[a-z0-9_]{1,32}", k) and (k in desc or not any("{arg}" in x for x in v))]
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
    ask = {"mode": "text" if a.text_mode else "buttons", "options": options, "created": time.time(),
           "thread": env_thread()}
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
    t = env_thread()
    if t is not None and not a.all:                               # во вкладке — только пересланное в неё
        files = [f for f in files if json.loads(f.read_text(encoding="utf-8")).get("thread") == t]
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
    descs = load_command_meta()
    if a.op == "rm":
        acts.pop(a.name, None)
        descs.pop(a.name, None)
    else:
        argv = list(a.argv)
        while argv[:1] in (["--desc"], ["--hint"]) and len(argv) > 1:   # NAME --desc "…" --hint "…" -- argv...
            key, limit = (("desc", 256) if argv[0] == "--desc" else ("hint", 64))
            descs.setdefault(a.name, {})[key] = argv[1][:limit]
            argv = argv[2:]
        argv = argv[1:] if argv and argv[0] == "--" else argv
        if not argv:
            die("tg action add NAME -- argv... ({arg} — аргумент кнопки)")
        if not Path(argv[0]).is_absolute():
            die("argv[0] — абсолютный путь (демон под systemd не видит твой PATH)")
        acts[a.name] = argv
    CONF.mkdir(parents=True, exist_ok=True)
    write_atomic(CONF / "actions.json", json.dumps(acts, ensure_ascii=False, indent=1))
    write_atomic(CONF / "commands.json", json.dumps(descs, ensure_ascii=False, indent=1))
    print(f"OK: {a.op} {a.name}")


def cmd_topic(a):
    """Новая вкладка; печатает её id (export TG_THREAD=…). --cwd: где продолжать сессию этой вкладки.
    tg topic rm ID... — удалить вкладки (вместе с перепиской в них) и их записи в threads.json."""
    if a.name == "rm":
        api, t, rc = Api(), threads(), 0
        for tid in a.ids:
            try:
                api.call("deleteForumTopic", chat_id=chat_id(), message_thread_id=int(tid))
                print(f"удалена {tid} «{t.get(str(tid), {}).get('name', '')}»")
            except Exception as e:                                 # уже удалена руками — запись всё равно чистим
                if not re.search(r"not found|thread_not_found|topic_id_invalid", str(e), re.I):
                    print(f"ERR {tid}: {e}", file=sys.stderr)
                    rc = 1
                    continue
                print(f"не найдена в Telegram {tid}, убираю запись")
            t.pop(str(tid), None)
            shutil.rmtree(DATA / "sessions" / str(tid), ignore_errors=True)
        write_atomic(d("threads.json"), json.dumps(t, ensure_ascii=False, indent=1))
        sys.exit(rc)
    print(create_topic(Api(), a.name, **({"cwd": a.cwd} if a.cwd else {}), kind=a.kind))


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
    tp = sp.add_parser("topic"); tp.add_argument("name"); tp.add_argument("ids", nargs="*", help="для rm: id вкладок")
    tp.add_argument("--cwd"); tp.add_argument("--kind", default="task")
    tp.set_defaults(f=cmd_topic)
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
    i.add_argument("--all", action="store_true", help="все вкладки, а не только TG_THREAD")
    i.set_defaults(f=cmd_inbox)
    ac = sp.add_parser("action")
    ac.add_argument("op", choices=["add", "list", "rm"]); ac.add_argument("name", nargs="?")
    ac.add_argument("argv", nargs=argparse.REMAINDER)
    ac.set_defaults(f=cmd_action)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
