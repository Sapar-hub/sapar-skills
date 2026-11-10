#!/usr/bin/env python3
"""Раннер задач из чата: `claude -p` с живым статусом в Telegram, до `parallel` задач одновременно. Запускает демон.

Конфиг ~/.config/tg-bridge/agent.json:
  {
    "claude": "/home/u/.local/bin/claude",
    "cwd": "/home/u",
    "parallel": 3,                                       # одновременно задач (по одной на вкладку)
    "allowed_tools": ["Bash(tg:*)", "Read", ...],
    "add_dirs": ["/home/u/.local/share/job-apply"],
    "env_file": "/home/u/.config/job-apply/worker.env",   # прокси и пр. (у демона под systemd их нет)
    "runs_dir": "/home/u/.local/share/job-apply/runs",    # регистрация прогона для лока браузера (job-apply lock.mjs)
    "permission_mode": "acceptEdits"
  }
Очередь: ~/.local/share/tg-bridge/tasks/*.json. Идут сейчас: running/*.json (для /status и /stop).
Вкладки (Threaded Mode): задача из общего чата получает новую вкладку и новую сессию; задача внутри вкладки продолжает
её сессию (sessions/<thread>, cwd вкладки из threads.json). Две задачи одной вкладки параллельно не идут.
Браузер — не здесь: его берёт лениво первый же вызов browser.mjs/hh.mjs прогона (JOB_APPLY_RUN), остальные ждут.
"""
import atexit, json, os, secrets, shutil, signal, subprocess, sys, threading, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF = Path(os.environ.get("TG_BRIDGE_CONFIG", "~/.config/tg-bridge")).expanduser()
DATA = Path(os.environ.get("TG_BRIDGE_DATA", "~/.local/share/tg-bridge")).expanduser()
RULES = HERE.parent / "skills" / "tg-bridge" / "references" / "remote.md"
TG = [sys.executable, "-I", str(HERE / "tg.py")]
FORWARD_TASK = ("Пользователь переслал в inbox новые сообщения ({n}) без комментария. Прочитай их (`tg inbox`). "
                "Если в этой сессии ты ждал пересланное (например, вопросы, на которые обещал набросать ответы), "
                "сделай обещанное. Если контекста нет, коротко скажи, что это и что с этим можно сделать.")


def tg(*args, env=None):
    return subprocess.run([*TG, *args], capture_output=True, text=True, env=env)


def load_env(path):
    env = dict(os.environ)
    if path and Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    env["PATH"] = f"{Path.home()}/.local/bin:{env.get('PATH', '/usr/bin')}"
    return env


def tab_key(task, path):
    return str(task.get("thread") or f"new:{path.name}")         # новая задача из общего чата — своя будущая вкладка


def pick(tdir, busy):
    """Готовая задача, чья вкладка свободна. Текст во вкладке поглощает её пересылки (агент всё равно читает inbox);
    пересылка ждёт not_before. -> (path, task) или None (сейчас нечего брать)."""
    files = sorted(tdir.glob("*.json"))
    fwds = [f for f in files if f.name.startswith("forward")]
    for f in (f for f in files if f not in fwds):
        task = json.loads(f.read_text())
        if tab_key(task, f) in busy:
            continue
        (tdir / f"forward-{task.get('thread') or 'general'}.json").unlink(missing_ok=True)
        return f, task
    for f in fwds:
        task = json.loads(f.read_text())
        if task.get("not_before", 0) > time.time() or (task.get("thread") and str(task["thread"]) in busy):
            continue
        return f, {**task, "text": FORWARD_TASK.format(n=task.get("count", 1)),
                   "title": f"пересланное: {task.get('count', 1)}"}
    return None


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
    r = tg("topic", name)
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        return None                                                # Threaded Mode выключен — работаем в общем чате
    tid = int(r.stdout.strip())
    for f in (DATA / "inbox").glob("*.json"):
        it = json.loads(f.read_text())
        if it.get("thread") is None:
            it["thread"] = tid
            f.write_text(json.dumps(it, ensure_ascii=False))
    return tid


def run(cfg, task, key):
    tid = open_tab(task)
    in_tab = bool(task.get("thread"))                              # писали во вкладке: статус — ответом на сообщение
    sess_file = DATA / "sessions" / f"{tid or 'general'}"
    sess_file.parent.mkdir(parents=True, exist_ok=True)
    cwd = load_threads().get(str(tid), {}).get("cwd") or cfg.get("cwd", str(Path.home()))
    title = task.get("title") or " ".join(task["text"].split())[:60]
    env = load_env(cfg.get("env_file"))
    if tid:
        env["TG_THREAD"] = str(tid)
    # регистрация прогона для лока браузера: pid раннера живёт весь прогон
    run_id = f"tg-{tid or 'general'}-{secrets.token_hex(3)}"
    runs = Path(cfg.get("runs_dir") or Path.home() / ".local/share/job-apply/runs")
    runs.mkdir(parents=True, exist_ok=True)
    (runs / run_id).write_text(f"{os.getpid()}\n{title}")
    env["JOB_APPLY_RUN"] = run_id
    info = DATA / "running" / f"{key.replace(':', '_')}.json"
    info.parent.mkdir(parents=True, exist_ok=True)
    try:
        for attempt in (1, 2):
            sid = sess_file.read_text().strip() if sess_file.exists() else None
            cmd = [cfg.get("claude", "claude"), "-p", prompt(task["text"]), "--output-format", "stream-json",
                   "--verbose", "--permission-mode", cfg.get("permission_mode", "acceptEdits")]
            if cfg.get("allowed_tools"):
                cmd += ["--allowedTools", *cfg["allowed_tools"]]
            for dd in cfg.get("add_dirs", []):
                cmd += ["--add-dir", dd]
            if sid:
                cmd += ["--resume", sid]
            log = DATA / "agent-logs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{tid or 'general'}.jsonl"
            log.parent.mkdir(parents=True, exist_ok=True)
            # через fish: config.fish экспортирует креды (KC_*, MAILRU_*), которых нет в окружении systemd --user
            if cfg.get("login_shell", "fish") == "fish" and shutil.which("fish"):
                cmd = [shutil.which("fish"), "-c", "exec $argv", *cmd]
            claude = subprocess.Popen(cmd, cwd=cwd, env=env, start_new_session=True,  # своя группа: /stop во вкладке
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            info.write_text(json.dumps({"thread": tid, "title": title, "started": time.time(), "pgid": claude.pid},
                                       ensure_ascii=False))
            # без шапки: статус — ответ на сообщение с задачей или первое сообщение вкладки, названной по задаче
            subprocess.run([*TG, "progress", "--log", str(log), "--freeform",
                            "--session-file", str(sess_file),
                            *(["--reply-to", str(task["message_id"])] if in_tab or not tid else [])],
                           stdin=claude.stdout, capture_output=True, text=True, env=env)
            claude.wait()
            if sid and attempt == 1 and "No conversation found" in log.read_text(errors="ignore"):
                sess_file.unlink(missing_ok=True)                  # сессию не нашли — один раз заново
                continue
            return
    finally:
        info.unlink(missing_ok=True)
        (runs / run_id).unlink(missing_ok=True)
        owner = runs.parent / "browser.owner"                     # отпустить браузер, если держали
        try:
            if owner.read_text().strip() == run_id:
                owner.unlink()
        except OSError:
            pass


def stop_all():
    """claude у каждой задачи в своей группе процессов — гасим их явно, иначе переживут раннер."""
    for f in (DATA / "running").glob("*.json"):
        try:
            os.killpg(json.loads(f.read_text())["pgid"], signal.SIGTERM)
        except (OSError, ValueError, KeyError):
            pass


def main():
    cfg = json.loads((CONF / "agent.json").read_text())
    pidf = DATA / "agent.pid"
    pidf.write_text(str(os.getpid()))
    atexit.register(lambda: pidf.unlink(missing_ok=True))
    signal.signal(signal.SIGTERM, lambda *_: (stop_all(), sys.exit(143)))
    limit = int(cfg.get("parallel", 3))
    running = {}                                                   # tab key -> Thread
    tdir = DATA / "tasks"
    tdir.mkdir(parents=True, exist_ok=True)
    idle_since = None
    while True:
        running = {k: t for k, t in running.items() if t.is_alive()}
        got = pick(tdir, set(running)) if len(running) < limit else None
        if got:
            path, task = got
            key = tab_key(task, path)
            path.unlink(missing_ok=True)                           # взяли: /stop не перезапустит её
            t = threading.Thread(target=run, args=(cfg, task, key), daemon=True)
            t.start()
            running[key] = t
            idle_since = None
            continue
        if not running and not any(tdir.glob("*.json")):
            idle_since = idle_since or time.time()
            if time.time() - idle_since > 2:                       # пусто — выходим, демон запустит заново
                return
        time.sleep(1)


if __name__ == "__main__":
    main()
