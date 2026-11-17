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
    "permission_mode": "acceptEdits",
    "general": "single",                                 # "single" (по умолчанию): общий чат = одна сессия, без
                                                         #   новых вкладок; "tabs": каждая задача общего чата — вкладка
    "engine": "claude",                                  # запасной движок: "opencode" (тот же раннер, тот же статус)
    "opencode": {"bin": "/home/u/.opencode/bin/opencode", "config_dir": "/home/u/.config/tg-bridge/opencode-agent",
                 "model": "qwen/qwen3.8-max"}
  }
Движок можно менять на лету: раннер живёт только пока есть задачи. Конфиг OpenCode-агента собирается из этого же
allowed_tools: `agent.py opencode-config [--provider-from <opencode.jsonc>]` (права — legacy-ключ `permission`:
массив `permissions` в `opencode run` 2.3 не применяется, проверено).
Очередь: ~/.local/share/tg-bridge/tasks/*.json. Идут сейчас: running/*.json (для /status и /stop).
Вкладки (Threaded Mode): задача внутри вкладки продолжает её сессию (sessions/<thread>, cwd из threads.json).
Общий чат: при "general": "single" все его задачи — одна сессия (sessions/general) прямо в общем чате, по очереди;
новая вкладка — только по /new <задача> (task.new_tab). Две задачи одной вкладки параллельно не идут.
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


def general_single():
    try:
        return json.loads((CONF / "agent.json").read_text()).get("general", "single") != "tabs"
    except (OSError, json.JSONDecodeError):
        return True


def tab_key(task, path):
    if task.get("thread"):
        return str(task["thread"])
    if not task.get("new_tab") and general_single():
        return "general"                                           # общий чат — одна сессия, задачи по очереди
    return f"new:{path.name}"                                      # /new — своя будущая вкладка


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
    if not task.get("new_tab") and general_single():
        return None                                                # отвечаем в общем чате, сессия sessions/general
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


def read_session(f):
    """sessions/<вкладка>: {"engine": …, "id": …}; старый голый id — сессия claude."""
    if not f.exists():
        return None, None
    raw = f.read_text().strip()
    try:
        d = json.loads(raw)
        return d.get("engine"), d.get("id")
    except json.JSONDecodeError:
        return ("claude", raw) if raw else (None, None)


def opencode_config(cfg, provider_from=None):
    """Конфиг OpenCode-агента из allowed_tools (Claude-синтаксис): Bash(x:*) → permission.bash «x *»: allow,
    Read/Write/Edit/Glob/Grep/Skill/WebFetch/WebSearch → allow; остальное deny. Без «ask»: в headless некому ответить."""
    oc = cfg.get("opencode", {})
    out_dir = Path(oc.get("config_dir") or CONF / "opencode-agent").expanduser()
    tools = cfg.get("allowed_tools", [])
    bash = {"*": "deny"}
    for t in tools:
        m = __import__("re").match(r"Bash\(([^:)]+):\*\)", t)
        if m:
            bash[f"{m.group(1)} *"] = "allow"
            bash[m.group(1)] = "allow"
    has = lambda n: "allow" if n in tools else "deny"
    perm = {"bash": bash, "read": has("Read"), "edit": "allow" if {"Write", "Edit"} & set(tools) else "deny",
            "glob": has("Glob"), "grep": has("Grep"), "list": has("Glob"), "skill": has("Skill"),
            "webfetch": has("WebFetch"), "websearch": has("WebSearch"), "task": "deny", "todowrite": "allow",
            "external_directory": "allow"}
    plugins = HERE.parent.parent                                   # sapar-skills/plugins
    skills = [str(d) for d in sorted(plugins.glob("*/skills")) if d.is_dir()]
    skills.append(str(Path.home() / ".claude" / "skills"))
    conf = {"$schema": "https://opencode.ai/config.json", "snapshot": False,
            "model": oc.get("model", "opencode/deepseek-v4.1-flash-free"), "skills": skills, "permission": perm}
    if provider_from:                                              # свой провайдер (ключ — из env, не в файле)
        import re as _re
        src = _re.sub(r"^\s*//.*$", "", Path(provider_from).expanduser().read_text(), flags=_re.M)
        prov = json.loads(src).get("provider")
        if prov:
            conf["provider"] = prov
    out_dir.mkdir(parents=True, exist_ok=True)
    f = out_dir / "opencode.jsonc"
    f.write_text(json.dumps(conf, ensure_ascii=False, indent=2))   # собрано из agent.json — правь там и пересобери
    return f


def build_cmd(cfg, task, sess_file):
    """-> (engine, cmd, env_extra). Сессия продолжается, только если записана тем же движком."""
    engine = cfg.get("engine", "claude")
    s_engine, sid = read_session(sess_file)
    sid = sid if s_engine == engine else None
    if engine == "opencode":
        oc = cfg.get("opencode", {})
        cmd = [oc.get("bin") or shutil.which("opencode") or str(Path.home() / ".opencode/bin/opencode"),
               "run", "--standalone", "--format", "json"]      # standalone: иначе фоновый сервис со СВОИМ конфигом
        if oc.get("model"):
            cmd += ["-m", oc["model"]]
        if sid:
            cmd += ["--session", sid]
        cmd.append(prompt(task["text"]))
        return engine, cmd, {"OPENCODE_CONFIG_DIR": str(Path(oc.get("config_dir") or CONF / "opencode-agent").expanduser())}
    cmd = [cfg.get("claude", "claude"), "-p", prompt(task["text"]), "--output-format", "stream-json",
           "--verbose", "--permission-mode", cfg.get("permission_mode", "acceptEdits")]
    if cfg.get("allowed_tools"):
        cmd += ["--allowedTools", *cfg["allowed_tools"]]
    for dd in cfg.get("add_dirs", []):
        cmd += ["--add-dir", dd]
    if sid:
        cmd += ["--resume", sid]
    return engine, cmd, {}


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
            engine, cmd, extra = build_cmd(cfg, task, sess_file)
            sid = read_session(sess_file)[1] if read_session(sess_file)[0] == engine else None
            env.update(extra)
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
            subprocess.run([*TG, "progress", "--log", str(log), "--freeform", "--source", engine,
                            "--session-file", str(sess_file),
                            *(["--reply-to", str(task["message_id"])] if in_tab or not tid else [])],
                           stdin=claude.stdout, capture_output=True, text=True, env=env)
            claude.wait()
            dead = "No conversation found" if engine == "claude" else "Session not found"
            if sid and attempt == 1 and dead.lower() in log.read_text(errors="ignore").lower():
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
    """claude/opencode у каждой задачи в своей группе процессов — гасим их явно, иначе переживут раннер:
    SIGTERM, и через 5 с SIGKILL тем, кто ещё жив (OpenCode со своим сервером выходит не сразу)."""
    groups = []
    for f in (DATA / "running").glob("*.json"):
        try:
            pg = json.loads(f.read_text())["pgid"]
            os.killpg(pg, signal.SIGTERM)
            groups.append(pg)
        except (OSError, ValueError, KeyError):
            pass
    if groups:
        time.sleep(5)
        for pg in groups:
            try:
                os.killpg(pg, signal.SIGKILL)
            except OSError:
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
    if sys.argv[1:2] == ["opencode-config"]:
        c = json.loads((CONF / "agent.json").read_text())
        pf = sys.argv[sys.argv.index("--provider-from") + 1] if "--provider-from" in sys.argv else None
        print(opencode_config(c, pf))
    else:
        main()
