#!/usr/bin/env python3
"""Отклик по этапам: исполнитель (OpenCode, бесплатно) работает руками, Claude решает и пишет, код проверяет.

  pipeline.py <ключ очереди>            один элемент queue.tsv (worker.sh зовёт это вместо монолитного claude -p)

Этапы (каждый виден строкой в одном статус-сообщении Telegram, во вкладке отклика):
  1. разведка   — исполнитель: вакансия на сайте работодателя, live/precheck, открыть форму. Итог: RECON: ok <url>
                  код снимает vacancy.md (текст) и form-snap.txt (поля формы)
  2. план       — claude -p, без браузера: tailored-резюме (check + render), fill-plan.json, tracker-row.json,
                  при нехватке данных — tg ask. Итог: PLAN: ok | skip — причина
  3. заполнение — исполнитель строго по fill-plan.json, без отправки
  4. проверка   — verify.py: форма = план (иначе заполнение ещё раз, потом пауза)
  5. гейт       — gate.py: скрин + PDF + значения из плана → «отправить» в Telegram → одноразовый токен
  6. отправка   — КОД, не модель: кнопка отправки из снимка формы, click --token, проверка «спасибо/отправлено»
  7. учёт       — applied.tsv, job_tracker (tracker.py: web app или outbox)
Итог в stdout последней строкой: STATUS: done|skipped|paused|failed — … (как у старого worker'а).

Окружение: JOB_APPLY_RUN и TG_THREAD ставит worker.sh. Исполнитель: OpenCode с изолированным конфигом
~/.config/job-apply/opencode-executor (allowlist), из чистой папки в /tmp, с таймаутом и одним повтором.
"""
import json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
HOME = Path(os.environ.get("JOB_APPLY_HOME", "~/.local/share/job-apply")).expanduser()
CONF = Path(os.environ.get("JOB_APPLY_CONFIG", "~/.config/job-apply")).expanduser()
EXEC_CONF = CONF / "opencode-executor"
OPENCODE = shutil.which("opencode") or str(Path.home() / ".opencode/bin/opencode")
CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
B = f"node {HERE}/browser.mjs"
SUCCESS = re.compile(r"спасибо|отклик (отправлен|принят|получен)|мы получили|успешно|заявк[аи] (отправлен|принят)|"
                     r"thank you|application (sent|received|submitted)", re.I)


# --- статус в Telegram: одно сообщение на отклик, правится по ходу ---------------------------------------------

class Status:
    def __init__(self, title):
        self.title, self.lines, self.t0, self.mid = title, [], time.time(), None
        r = subprocess.run(["tg", "send", "--html", f"<b>{esc(title)}</b>\nзапуск"], capture_output=True, text=True)
        self.mid = r.stdout.strip() if r.returncode == 0 else None

    def step(self, name, state="идёт"):
        self.lines = [l for l in self.lines if l[0] != name] + [(name, state, time.time())]
        self.render()

    def render(self, tail=""):
        if not self.mid:
            return
        body = "\n".join(f"{n}: {s}" for n, s, _ in self.lines)
        text = f"<b>{esc(self.title)}</b>\n{mmss(time.time() - self.t0)}\n\n{esc(body)}" + (f"\n\n{esc(tail)}" if tail else "")
        subprocess.run(["tg", "edit", self.mid, "--html", text[:4000]], capture_output=True)


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def mmss(s):
    return f"{int(s // 60)}:{int(s % 60):02d}"


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kw)


# --- исполнитель и планировщик -------------------------------------------------------------------------------

def executor(prompt, log, timeout=600, tries=2):
    """OpenCode из чистой папки с allowlist-конфигом. -> последний текст ответа ('' — не ответил)."""
    for attempt in range(1, tries + 1):
        work = tempfile.mkdtemp(prefix="ja-exec.")
        env = {**os.environ, "OPENCODE_CONFIG_DIR": str(EXEC_CONF)}
        timed_out = False
        try:
            p = subprocess.run([OPENCODE, "run", "--standalone", "--format", "json", prompt], cwd=work, env=env,
                               capture_output=True, text=True, timeout=timeout)
            out = p.stdout
        except subprocess.TimeoutExpired as e:
            timed_out = True
            out = (e.stdout or b"").decode(errors="ignore") if isinstance(e.stdout, bytes) else (e.stdout or "")
        Path(f"{log}.{attempt}.jsonl").write_text(out)
        texts, tools = [], 0
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "text":
                texts.append(ev.get("part", {}).get("text", ""))
            tools += ev.get("type") == "tool_use"
        if timed_out:
            # завис на старте (бесплатная модель так умеет) — повтор; работал и не уложился — повтор не поможет
            if tools >= 3:
                return f"TIMEOUT: {mmss(timeout)}, {tools} команд"
            continue
        if texts:
            return texts[-1]
    return ""


def planner(prompt, appdir, log, timeout=900):
    """claude -p без браузера: только чтение, запись в папку отклика, resume.mjs, tg. -> финальный текст."""
    allowed = ["Read", "Write", "Edit", "Glob", "Grep", f"Bash(node {HERE}/resume.mjs:*)", "Bash(tg:*)",
               "Bash(pdftoppm:*)"]
    cmd = [CLAUDE, "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
           "--allowedTools", *allowed, "--add-dir", str(appdir), "--add-dir", str(CONF), "--add-dir", str(HOME)]
    if shutil.which("fish"):                                     # креды из config.fish (как у остальных прогонов)
        cmd = [shutil.which("fish"), "-c", "exec $argv", *cmd]
    try:
        p = subprocess.run(cmd, cwd=SKILL, capture_output=True, text=True, timeout=timeout)
        Path(log).write_text(p.stdout + p.stderr)
        res = json.loads(p.stdout or "{}")
        return res.get("result", "") if not res.get("is_error") else f"ERROR: {res.get('result', '')}"
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return f"ERROR: {e}"


def last_line(text, tag):
    m = re.findall(rf"^{tag}:\s*(.*)$", text or "", re.M)
    return m[-1].strip() if m else ""


# --- этапы --------------------------------------------------------------------------------------------------

RECON = """Ты исполнитель. Разведка вакансии для отклика. Ничего не заполняй и не отправляй.
Разрешены ТОЛЬКО эти команды (другие запрещены, не пробуй их и не пиши свои скрипты):
  {B} goto <url> | text 8000 | snap | click <ref> | live <url> | press Enter | fill <ref> <текст>
  node {H}/precheck.mjs --company '<компания>' --title '<должность>' --url '<url>'
Вакансия из дайджеста: «{title}» ({details}). Ссылка агрегатора: {href}. Пост: {post}.
1. Найди ЭТУ вакансию на сайте САМОГО работодателя (карьерный сайт компании; поиск на нём — через fill+press Enter).
   Агрегаторы (changellenge, youngjunior, hirify, hh) — только чтобы узнать компанию и название.
2. {B} live <url> и precheck.
3. Открой форму отклика (кнопка «Откликнуться» и т.п. на странице вакансии; cookie-баннеры закрывай).
   Внутри формы ничего не нажимай и не заполняй. Оставь форму открытой на экране.
Бюджет: не больше 30 команд. Сайт не открывается (ERR_TUNNEL/timeout) — повтори один раз, потом иди дальше.
Если у работодателя нет своей формы, но вакансия есть на hh.ru, — это RECON: hh, не ищи дальше.
Последняя строка ответа ровно одна из:
RECON: ok <url вакансии на сайте работодателя> | <компания> | <должность>
RECON: hh <url вакансии на hh.ru> | <компания> | <должность>
RECON: closed — <почему>
RECON: blacklist — <что сказал precheck>
RECON: dup — <что сказал precheck>
RECON: not_found — <где искал>"""

PLAN = """Скилл job-apply, этап ПЛАН конвейера (браузера нет, форма уже открыта исполнителем). Сначала прочитай SKILL.md
(правила: только факты из master.yaml, без зарплаты, резюме под вакансию). Папка отклика: {appdir}
Входные файлы: {appdir}/vacancy.md (текст вакансии + live/precheck), {appdir}/form-snap.txt (поля формы: ref тип «подпись» *=обязательное).
Сделай:
1. Tailored-резюме в {appdir}/<Фамилия>_<Имя>_<TRACK>.yaml → node {H}/resume.mjs check (исправь всё, что не из мастера)
   → node {H}/resume.mjs render → PDF рядом. Посмотри PNG (pdftoppm -r 70 -png).
2. {appdir}/fill-plan.json:
   {{"url": "<url вакансии>", "company": "...", "title": "...", "resume_pdf": "<абсолютный путь PDF>",
     "cover_letter": "<текст или пусто>",
     "fields": [{{"label": "<подпись поля как в form-snap>", "value": "<точное значение>"}},
                {{"label": "...", "upload": "<абсолютный путь PDF>"}}, {{"label": "...", "check": true}},
                {{"label": "...", "select": "<пункт>"}}]}}
   Каждое заполняемое поле формы — ровно один пункт, значения дословные (телефон — как в профиле). Поля, которые
   не нужны, не включай. Нет данных для обязательного поля — спроси: tg ask "<вопрос>" --text (или --buttons).
3. {appdir}/tracker-row.json: {{"A": компания, "B": должность, "C": "{today}", "D": "Отклик", "E": "Ожидание ответа",
   "F": "<+3 раб. дня: что дальше>", "G": "Уточнить", "H": "<заметки>", "I": url, "J": "<папка/PDF>",
   "K": "<сопроводительное>", "L": "<все поля формы: значения>", "M": "<источник: {feed} → сайт работодателя>", "N": ""}}
Последняя строка: PLAN: ok   или   PLAN: skip — <почему не подаваться>"""

FILL = """Ты исполнитель. ЗАПОЛНИ открытую форму отклика СТРОГО по плану {appdir}/fill-plan.json. ОТПРАВЛЯТЬ НЕЛЬЗЯ
(кнопка отправки всё равно заблокирована без подтверждения пользователя — не пытайся).
Разрешены ТОЛЬКО: {B} snap | fill <ref> <текст> | fill --type <ref> <текст> | upload <ref> <файл> | check <ref> |
  select <ref> <пункт> | antselect <ref> <текст> | pick <текст> | click <ref> (только баннеры/раскрыть поле) | shot | text 3000
  и чтение файла плана. Свои скрипты, curl и т.п. запрещены.
Правила: значения только из плана, дословно. В поле — только значение из плана: не вводи пробные/тестовые значения.
Если маска съедает ввод через fill --type — один раз попробуй обычный fill. Обязательное поле, которого нет в плане,
или поле плана, которого нет на странице, — НЕ заполняй догадкой, остановись.
{extra}После заполнения сделай snap и сверь значения.
Последняя строка: FILL: ok   или   FILL: mismatch — <какое поле и почему>"""


def run(key):
    q = json.loads(sh([sys.executable, "-I", str(HERE / "queue.py"), "show", key]).stdout)
    slug = re.sub(r"[^\w]+", "-", q["title"].lower())[:50].strip("-") or key
    appdir = HOME / "applications" / f"q{key.replace('.', '-')}-{slug}"
    appdir.mkdir(parents=True, exist_ok=True)
    logs = appdir / "logs"
    logs.mkdir(exist_ok=True)
    global ST
    st = ST = Status(q["title"])
    today = time.strftime("%Y-%m-%d")

    # 1. разведка
    st.step("разведка")
    out = executor(RECON.format(B=B, H=HERE, **q), logs / "1-recon")
    r = last_line(out, "RECON")
    if not r.startswith("ok"):
        st.step("разведка", r or "исполнитель не ответил")
        kind = r.split()[0] if r else ""
        # paused — только когда ждём человека у открытой формы; сбой разведки очередь не держит
        status = {"closed": "skipped", "blacklist": "skipped", "dup": "skipped", "not_found": "skipped"}.get(kind, "failed")
        if kind == "hh":
            note = f"только hh.ru: {r[3:]} — отклик через hh.mjs (worker hh пока не делает)"
            return "failed", note
        return status, f"разведка: {r or last_line(out, 'TIMEOUT') or 'нет ответа'}"
    url = r.split()[1] if len(r.split()) > 1 else ""
    vac = sh(f"{B} text 20000").stdout
    live = sh(f"{B} live {url}").stdout if url else ""
    (appdir / "vacancy.md").write_text(f"URL: {url}\nRECON: {r}\n\nlive:\n{live}\n\n{vac}", encoding="utf-8")
    (appdir / "form-snap.txt").write_text(sh(f"{B} snap").stdout, encoding="utf-8")
    st.step("разведка", "готово")

    # 2. план (Claude)
    st.step("план и резюме")
    out = planner(PLAN.format(appdir=appdir, H=HERE, today=today, feed=q["feed"]), appdir, logs / "2-plan.json")
    p = last_line(out, "PLAN")
    if p.startswith("skip"):
        st.step("план и резюме", p)
        return "skipped", p
    if not p.startswith("ok") or not (appdir / "fill-plan.json").exists():
        st.step("план и резюме", "ошибка")
        return "failed", f"план: {out[-200:]}"
    st.step("план и резюме", "готово")

    # 3–5. заполнение → проверка → гейт (с правками пользователя по кругу)
    extra = ""
    for round_ in range(1, 4):
        st.step("заполнение")
        out = executor(FILL.format(B=B, appdir=appdir, extra=extra), logs / f"3-fill-{round_}")
        f = last_line(out, "FILL")
        st.step("заполнение", "готово" if f.startswith("ok") else (f or "нет ответа"))
        v = sh([sys.executable, "-I", str(HERE / "verify.py"), str(appdir)])
        st.step("проверка формы", "совпадает с планом" if v.returncode == 0 else "расхождение")
        if v.returncode:
            if round_ < 2:
                extra = f"Прошлая попытка не совпала с планом:\n{v.stdout}\nИсправь именно это.\n"
                continue
            return "paused", f"форма не совпала с планом: {v.stdout.strip()[:300]}"
        st.step("подтверждение", "жду тебя в чате")
        g = sh([sys.executable, "-I", str(HERE / "gate.py"), str(appdir)])
        if g.returncode == 0:
            token = last_line(g.stdout, "TOKEN")
            break
        if g.returncode == 4:                                    # правка: планировщик переделывает план
            edit = last_line(g.stdout, "EDIT")
            st.step("подтверждение", f"правка: {edit[:80]}")
            out = planner(f"Пользователь просит поправить отклик: «{edit}». Обнови {appdir}/fill-plan.json "
                          f"(и резюме/письмо, если нужно) по правилам SKILL.md. Последняя строка: PLAN: ok",
                          appdir, logs / f"2-plan-edit-{round_}.json")
            extra = "План обновлён после правки пользователя: перезаполни изменившиеся поля.\n"
            continue
        st.step("подтверждение", {2: "нет ответа", 3: "форма не совпала", 5: "стоп"}.get(g.returncode, "ошибка"))
        return "paused", {2: "нет ответа на подтверждение", 5: "пользователь остановил"}.get(g.returncode, g.stdout[-200:])
    else:
        return "paused", "слишком много правок"

    # 6. отправка — код
    st.step("отправка")
    snap = sh(f"{B} snap").stdout
    btn = [m.group(1) for m in re.finditer(r'^(\S+) button "([^"]*)"', snap, re.M)
           if re.search(r"откликн|отправ|подать|submit|apply|send", m.group(2), re.I)]
    if not btn:
        return "paused", "не нашёл кнопку отправки"
    c = sh(f"{B} click {btn[-1]} --token {token}", env={**os.environ})
    time.sleep(4)
    after = sh(f"{B} text 4000").stdout
    sh(f"{B} shot {appdir}/after-submit.png")
    if c.returncode or not SUCCESS.search(after):
        subprocess.run(["tg", "send", "Не уверен, что отклик ушёл: на странице нет подтверждения. Проверь сам.",
                        "--photo", str(appdir / "after-submit.png")], capture_output=True)
        st.step("отправка", "не подтверждено")
        return "paused", "отправка не подтверждена сайтом — проверить вручную, НЕ отправлять повторно"
    st.step("отправка", "сайт подтвердил")

    # 7. учёт
    plan = json.loads((appdir / "fill-plan.json").read_text())
    with open(HOME / "applied.tsv", "a", encoding="utf-8") as fh:
        fh.write("\t".join([today, plan["company"], plan["title"], plan["url"],
                            str(Path(plan.get("resume_pdf", "")).relative_to(HOME / "applications"))
                            if plan.get("resume_pdf", "").startswith(str(HOME / "applications")) else plan.get("resume_pdf", ""),
                            "отправлено"]) + "\n")
    row = appdir / "tracker-row.json"
    t = sh([sys.executable, "-I", str(HERE / "tracker.py"), "add"], input=row.read_text()) if row.exists() else None
    st.step("трекер", (t.stdout.strip() if t else "нет строки")[:80])
    return "done", f"{plan['company']} {plan['title']}"


ST = None
WORDS = {"done": "отправлено", "skipped": "пропущено", "paused": "пауза, нужен ты", "failed": "ошибка"}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    try:
        status, note = run(sys.argv[1])
    except Exception as e:
        status, note = "failed", f"{type(e).__name__}: {e}"
    if ST:
        ST.render(tail=f"{WORDS.get(status, status)}: {note}")
    print(f"STATUS: {status} — {note}")


if __name__ == "__main__":
    main()
