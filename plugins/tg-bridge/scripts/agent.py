#!/usr/bin/env python3
"""Раннер задач из чата: по одной, `claude -p` с живым статусом в Telegram. Запускает демон (tg.py).

Конфиг ~/.config/tg-bridge/agent.json:
  {
    "claude": "/home/u/.local/bin/claude",
    "cwd": "/home/u",
    "allowed_tools": ["Bash(tg:*)", "Read", ...],
    "add_dirs": ["/home/u/.local/share/job-apply"],
    "env_file": "/home/u/.config/job-apply/worker.env",   # прокси и пр. (у демона под systemd их нет)
    "lock": "/home/u/.local/share/job-apply/worker.lock",  # общий с job-apply worker: браузер один
    "permission_mode": "acceptEdits"
  }
Очередь: ~/.local/share/tg-bridge/tasks/*.json.
Вкладки (Threaded Mode): задача из общего чата получает новую вкладку и новую сессию; задача внутри вкладки продолжает
её сессию (sessions/<thread>, cwd вкладки из threads.json — worker открывает вкладки в своей папке).
"""
import atexit, fcntl, json, os, shutil, signal, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF = Path(os.environ.get("TG_BRIDGE_CONFIG", "~/.config/tg-bridge")).expanduser()
DATA = Path(os.environ.get("TG_BRIDGE_DATA", "~/.local/share/tg-bridge")).expanduser()
RULES = HERE.parent / "skills" / "tg-bridge" / "references" / "remote.md"
TG = [sys.executable, "-I", str(HERE / "tg.py")]


def tg(*args, env=None):
    subprocess.run([*TG, *args], capture_output=True, env=env)


def load_env(path):
    env = dict(os.environ)
    if path and Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    env["PATH"] = f"{Path.home()}/.local/bin:{env.get('PATH', '/usr/bin')}"
    return env


FORWARD_TASK = ("Пользователь переслал в inbox новые сообщения ({n}) без комментария. Прочитай их (`tg inbox`). "
                "Если в этой сессии ты ждал пересланное (например, вопросы, на которые обещал набросать ответы), "
                "сделай обещанное. Если контекста нет, коротко скажи, что это и что с этим можно сделать.")


def next_task(tdir):
    """Следующая задача. Пересылка ждёт FORWARD_WAIT; если за это время во вкладке пришёл текст — берём текст,
    а пересылка этой вкладки растворяется в нём (агент всё равно читает inbox). -> (path, task) или None."""
    while True:
        files = sorted(tdir.glob("*.json"))
        if not files:
            return None
        fwds = [f for f in files if f.name.startswith("forward")]
        texts = [f for f in files if f not in fwds]
        if texts:
            task = json.loads(texts[0].read_text())
            (tdir / f"forward-{task.get('thread') or 'general'}.json").unlink(missing_ok=True)
            return texts[0], task
        ready = []
        for f in fwds:
            task = json.loads(f.read_text())
            if task.get("not_before", 0) <= time.time():
                ready.append((f, task))
        if ready:
            f, task = ready[0]
            return f, {**task, "text": FORWARD_TASK.format(n=task.get("count", 1)),
                       "title": f"пересланное: {task.get('count', 1)}"}
        time.sleep(1.1)


def prompt(text):
    return (f"Задача от пользователя из Telegram (он с телефона, терминала нет):\n\n{text}\n\n"
            f"Сначала прочитай правила удалённого режима: {RULES}. "
            f"Если задача про пересланное, оно лежит в `tg inbox`.")


def load_threads():
    f = DATA / "threads.json"
    return json.loads(f.read_text()) if f.exists() else {}


def open_tab(task):
    """Задача из общего чата → новая вкладка; пересланное туда из общего чата переезжает вместе с ней."""
    if task.get("thread"):
        return task["thread"]
    name = task.get("title") or " ".join(task["text"].split())[:60]
    if task.get("kind") == "forward":
        fwd = [json.loads(f.read_text()) for f in sorted((DATA / "inbox").glob("*.json"))]
        who = next(((i.get("forward_origin") or {}).get("name") for i in fwd if i.get("forward_origin")), None)
        name = f"пересланное: {who}" if who else name
    r = subprocess.run([*TG, "topic", name], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        return None                                                # Threaded Mode выключен — работаем в общем чате
    tid = int(r.stdout.strip())
    for f in (DATA / "inbox").glob("*.json"):                      # пересланное в общий чат — к этой задаче
        it = json.loads(f.read_text())
        if it.get("thread") is None:
            it["thread"] = tid
            f.write_text(json.dumps(it, ensure_ascii=False))
    return tid


def run(cfg, task):
    tid = open_tab(task)
    in_tab = bool(task.get("thread"))                              # писали во вкладке: статус — ответом на сообщение
    sess_file = DATA / "sessions" / f"{tid or 'general'}"
    sess_file.parent.mkdir(parents=True, exist_ok=True)
    cwd = load_threads().get(str(tid), {}).get("cwd") or cfg.get("cwd", str(Path.home()))
    env = load_env(cfg.get("env_file"))
    if tid:
        env["TG_THREAD"] = str(tid)
    for attempt in (1, 2):
        sid = sess_file.read_text().strip() if sess_file.exists() else None
        cmd = [cfg.get("claude", "claude"), "-p", prompt(task["text"]), "--output-format", "stream-json", "--verbose",
               "--permission-mode", cfg.get("permission_mode", "acceptEdits")]
        if cfg.get("allowed_tools"):
            cmd += ["--allowedTools", *cfg["allowed_tools"]]
        for dd in cfg.get("add_dirs", []):
            cmd += ["--add-dir", dd]
        if sid:
            cmd += ["--resume", sid]
        log = DATA / "agent-logs" / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        title = task.get("title") or " ".join(task["text"].split())[:60]
        # через fish: config.fish экспортирует креды (KC_*, MAILRU_*, прокси), которых нет в окружении systemd --user
        if cfg.get("login_shell", "fish") == "fish" and shutil.which("fish"):
            cmd = [shutil.which("fish"), "-c", "exec $argv", *cmd]
        claude = subprocess.Popen(cmd, cwd=cwd, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        prog = subprocess.run([*TG, "progress", "--title", title, "--log", str(log), "--freeform",
                               "--session-file", str(sess_file),
                               *(["--reply-to", str(task["message_id"])] if in_tab or not tid else [])],
                              stdin=claude.stdout, capture_output=True, text=True, env=env)
        claude.wait()
        # сессию не нашли (почистили ~/.claude) — один раз заново, без --resume
        if sid and attempt == 1 and "No conversation found" in log.read_text(errors="ignore"):
            sess_file.unlink(missing_ok=True)
            continue
        return prog.stdout


def main():
    cfg = json.loads((CONF / "agent.json").read_text())
    pidf = DATA / "agent.pid"
    pidf.write_text(str(os.getpid()))
    atexit.register(lambda: (pidf.unlink(missing_ok=True), (DATA / "agent-current.json").unlink(missing_ok=True)))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    lock = open(cfg["lock"], "a") if cfg.get("lock") else None
    while True:
        got = next_task(DATA / "tasks")
        if not got:
            return
        path, task = got
        path.unlink(missing_ok=True)                               # взяли: /stop не перезапустит её
        if lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                tg("send", "Жду: идёт другой прогон (отклик из очереди). Начну сразу после.",
                   "--reply-to", str(task["message_id"]), env={**os.environ, "TG_THREAD": str(task.get("thread") or "")})
                fcntl.flock(lock, fcntl.LOCK_EX)
        (DATA / "agent-current.json").write_text(json.dumps({**task, "started": time.time()}, ensure_ascii=False))
        try:
            run(cfg, task)
        finally:
            if lock:
                fcntl.flock(lock, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
