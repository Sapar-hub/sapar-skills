#!/usr/bin/env python3
"""Живой статус прогона агента в Telegram: одно сообщение, которое редактируется на месте.

  claude -p … --output-format stream-json --verbose | tg progress --title "<что делаем>" --log <file>
      [--freeform] [--session-file F] [--reply-to MESSAGE_ID]

--freeform      итог без строки STATUS: (задачи из чата): успех = прогон без ошибки
--session-file  сюда пишется session_id (для --resume следующей задачи)
--reply-to      статус — ответом на сообщение пользователя с задачей

- stdin: события stream-json; каждое пишется в --log как есть.
- В начале одно сообщение (одно уведомление), дальше только правки (без уведомлений), не чаще раза в EDIT_EVERY с.
- В конце то же сообщение становится итогом: статус, время, сводка агента. При ошибке полный лог приходит файлом.
- stdout: финальный текст агента (worker берёт из него строку STATUS: …).
"""
import argparse, html, json, re, subprocess, sys, time
from pathlib import Path

EDIT_EVERY, LAST_STEPS = 4.0, 8
WORDS = {"done": "готово", "skipped": "пропущено", "paused": "пауза, нужен ты", "failed": "ошибка",
         "bl-done": "добавлено в blacklist"}


TG = [sys.executable, "-I", str(Path(__file__).with_name("tg.py"))]


def tg(*args):
    p = subprocess.run([*TG, *args], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def step_of(block):
    """tool_use → короткая человеческая строка."""
    name, inp = block.get("name", ""), block.get("input") or {}
    if name == "Bash":
        cmd = inp.get("command", "")
        if re.match(r"\s*tg\s+ask\b", cmd):
            return "вопрос тебе в чат"
        if re.match(r"\s*tg\s+send\b", cmd):
            return "сообщение тебе в чат"
        return inp.get("description") or cmd.strip().splitlines()[0][:90]
    if name in ("Read", "Write", "Edit"):
        verb = {"Read": "читаю", "Write": "пишу", "Edit": "правлю"}[name]
        return f"{verb} {Path(inp.get('file_path', '')).name}"
    if name == "WebSearch":
        return f"поиск: {inp.get('query', '')[:70]}"
    if name == "WebFetch":
        return f"открываю {re.sub(r'^https?://', '', inp.get('url', ''))[:70]}"
    if name == "Skill":
        return f"скилл {inp.get('skill', '')}"
    if name in ("Glob", "Grep"):
        return f"ищу в файлах: {inp.get('pattern', '')[:60]}"
    return name.replace("mcp__claude_ai_", "").replace("__", ": ")


def plain(md):
    """Markdown агента → обычный текст (без **, `, заголовков)."""
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", md)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"^#+\s*", "", t, flags=re.M)
    return t.strip()


def mmss(sec):
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--freeform", action="store_true")
    ap.add_argument("--session-file")
    ap.add_argument("--reply-to")
    a = ap.parse_args()
    log = open(a.log, "w", encoding="utf-8")
    t0, steps, last_edit, result = time.time(), [], 0.0, None
    title = f"<b>{html.escape(a.title)}</b>"
    mid = tg("send", "--html", f"{title}\nзапуск", *(["--reply-to", a.reply_to] if a.reply_to else []))

    def render():
        lines = [f"{title}\nидёт, {mmss(time.time() - t0)}, шагов {len(steps)}", ""]
        lines += [f"· {html.escape(s)}" for s in steps[-LAST_STEPS:]]
        return "\n".join(lines)

    for raw in sys.stdin:
        log.write(raw)
        log.flush()
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if ev.get("session_id") and a.session_file:
            Path(a.session_file).write_text(ev["session_id"])
        if ev.get("type") == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use":
                    steps.append(step_of(b))
        elif ev.get("type") == "result":
            result = ev
        if mid and steps and time.time() - last_edit >= EDIT_EVERY:
            tg("edit", mid, "--html", render())
            last_edit = time.time()

    text = (result or {}).get("result") or ""
    m = re.search(r"^STATUS: (\w[\w-]*)\s*(?:—\s*(.*))?$", text, re.M)
    status = m.group(1) if m else ("done" if a.freeform and result else "failed")
    summary = plain(re.sub(r"^STATUS:.*$", "", text, flags=re.M)) or \
        ("агент не вернул итог" if result else "прогон оборвался, итога нет")
    if result and result.get("is_error"):
        status = "failed"
    head = f"{title}\n{WORDS.get(status, status)}, {mmss(time.time() - t0)}, шагов {len(steps)}"
    final = f"{head}\n\n{html.escape(summary)}"
    if mid:
        if len(final) > 4000:
            tg("edit", mid, "--html", head + "\n\nподробности ниже")
            tg("send", summary)
        else:
            tg("edit", mid, "--html", final)
    else:
        tg("send", "--html", final)
    if status == "failed":
        tg("send", "лог прогона", "--doc", a.log)
    log.close()
    print(text if (m or a.freeform) else f"{text}\nSTATUS: failed — {summary[:200]}")


if __name__ == "__main__":
    main()
