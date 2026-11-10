#!/usr/bin/env python3
"""Гейт отправки: проверить форму, спросить пользователя в Telegram, выдать одноразовый токен для submit.

  gate.py <папка отклика> [--timeout 900]

1. verify.py: форма = fill-plan.json, иначе стоп (exit 3, список расхождений).
2. Скрин формы + PDF + значения ИЗ ПЛАНА (текст собирает код, не модель) → tg ask «отправить / правка / стоп».
   Сайт в ~/.config/job-apply/auto.yaml no_confirm — без вопроса, но скрин и PDF всё равно уходят в чат.
3. «отправить» → токен в $JOB_APPLY_HOME/gate/$JOB_APPLY_RUN.token, stdout «TOKEN: <t>» (exit 0).
   Им и только им browser.mjs нажмёт кнопку отправки: `click <ref> --token <t>`. Токен одноразовый.
   «правка» → stdout «EDIT: <что поправить>» (exit 4). «стоп» → exit 5. Нет ответа → exit 2.
"""
import json, os, re, secrets, subprocess, sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("JOB_APPLY_HOME", "~/.local/share/job-apply")).expanduser()
CONF = Path(os.environ.get("JOB_APPLY_CONFIG", "~/.config/job-apply")).expanduser()


def tg(*args, check=False):
    return subprocess.run(["tg", *args], capture_output=True, text=True, check=check)


def no_confirm(url):
    f = CONF / "auto.yaml"
    if not f.exists():
        return False
    m = re.search(r"no_confirm:\s*\[([^\]]*)\]", f.read_text())
    hosts = [h.strip().strip("'\"") for h in (m.group(1).split(",") if m else []) if h.strip()]
    host = urlparse(url or "").hostname or ""
    return any(host == h or host.endswith("." + h) for h in hosts)


def summary(plan):
    lines = [f"{plan.get('company', '?')} — {plan.get('title', '?')}", plan.get("url", ""), ""]
    for f in plan.get("fields", []):
        if "value" in f or "select" in f:
            lines.append(f"{f['label']}: {f.get('value', f.get('select'))}")
        elif "upload" in f:
            lines.append(f"{f['label']}: {Path(f['upload']).name}")
        elif f.get("check"):
            lines.append(f"[x] {f['label'][:70]}")
    if plan.get("cover_letter"):
        lines += ["", "Сопроводительное:", plan["cover_letter"]]
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    d = Path(sys.argv[1])
    timeout = sys.argv[sys.argv.index("--timeout") + 1] if "--timeout" in sys.argv else "900"
    run = os.environ.get("JOB_APPLY_RUN")
    if not run:
        sys.exit("ERR: нет JOB_APPLY_RUN — гейт только для headless-прогона (у ноута отправляешь сам)")
    plan = json.loads((d / "fill-plan.json").read_text(encoding="utf-8"))

    v = subprocess.run([sys.executable, "-I", str(HERE / "verify.py"), str(d)], capture_output=True, text=True)
    if v.returncode:
        print(v.stdout.strip())
        tg("send", f"Форма не совпала с планом, отправку не предлагаю:\n{v.stdout.strip()}")
        sys.exit(3)

    shot = d / "gate-shot.png"
    subprocess.run(["node", str(HERE / "browser.mjs"), "shot", str(shot)], capture_output=True)
    text = summary(plan)
    pdf = plan.get("resume_pdf")
    if pdf and Path(pdf).exists():
        tg("send", "Резюме для этого отклика", "--doc", pdf)

    if no_confirm(plan.get("url")):
        tg("send", "Отправляю без вопроса (сайт в no_confirm):\n\n" + text[:900], "--photo", str(shot))
        answer = "отправить"
    else:
        r = tg("ask", text[:1000], "--photo", str(shot), "--buttons", "отправить,правка,стоп", "--timeout", timeout)
        if r.returncode == 2:
            sys.exit(2)
        answer = r.stdout.strip()

    if answer == "отправить":
        tok = secrets.token_hex(8)
        (HOME / "gate").mkdir(parents=True, exist_ok=True)
        f = HOME / "gate" / f"{run}.token"
        f.write_text(tok)
        os.chmod(f, 0o600)
        print(f"TOKEN: {tok}")
        sys.exit(0)
    if answer == "правка":
        r = tg("ask", "Что поправить?", "--text", "--timeout", timeout)
        print(f"EDIT: {r.stdout.strip()}" if r.returncode == 0 else "EDIT: (нет ответа)")
        sys.exit(4)
    if answer == "стоп":
        sys.exit(5)
    print(f"EDIT: {answer}")                     # свободный ответ текстом — тоже правка
    sys.exit(4)


if __name__ == "__main__":
    main()
