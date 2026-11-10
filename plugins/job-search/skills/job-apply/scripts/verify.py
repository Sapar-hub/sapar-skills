#!/usr/bin/env python3
"""Сверка заполненной формы с планом — кодом, а не на слово исполнителя.

  verify.py <папка отклика>          читает fill-plan.json, снимает `browser.mjs snap`, сравнивает
  verify.py <папка> --snap file.txt  сравнить с готовым снимком (тесты)

fill-plan.json:
  {"url": …, "company": …, "title": …, "resume_pdf": …, "cover_letter": …,
   "fields": [{"label": "Имя", "value": "Сапар"}, {"label": "Резюме", "upload": "/…/x.pdf"},
              {"label": "Согласие", "check": true}, {"label": "Город", "select": "Москва"}]}

Правила: каждое value/select есть в форме дословно; файл загружен (имя совпадает); нужные галочки стоят;
поле с непустым значением, которого нет в плане, — расхождение (исполнитель что-то дописал от себя).
Выход 0 = VERIFY: ok, 1 = VERIFY: mismatch (список проблем выше).
"""
import json, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LINE = re.compile(r'^(?P<ref>[fx]?\d*e\d+) (?P<kind>\S+) "(?P<label>[^"]*)"(?: value="(?P<value>[^"]*)")?(?P<rest>.*)$')


def snap():
    p = subprocess.run(["node", str(HERE / "browser.mjs"), "snap"], capture_output=True, text=True)
    if p.returncode:
        sys.exit(f"ERR: snap: {p.stderr.strip()}")
    return p.stdout


def parse(text):
    return [m.groupdict() for m in map(LINE.match, text.splitlines()) if m]


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def digits(s):
    return re.sub(r"\D", "", s or "")


def check(plan, items):
    problems, used = [], set()
    for f in plan.get("fields", []):
        label = f.get("label", "?")
        if "value" in f or "select" in f:
            want = str(f.get("value", f.get("select")))
            hit = [i for i in items if i["value"] is not None and (
                norm(i["value"]) == norm(want) or (digits(want) and len(digits(want)) >= 7 and
                                                   digits(i["value"])[-10:] == digits(want)[-10:]))]
            if not hit:
                problems.append(f"нет значения «{want}» ({label})")
            used.update(i["ref"] for i in hit)
        elif "upload" in f:
            name = Path(f["upload"]).name
            hit = [i for i in items if i["kind"] == "file" and name in (i["value"] or "")]
            if not hit:
                problems.append(f"файл не загружен: {name} ({label})")
            used.update(i["ref"] for i in hit)
        elif f.get("check"):
            hit = [i for i in items if i["kind"] in ("checkbox", "radio") and norm(label)[:25] in norm(i["label"])]
            if not hit or "[x]" not in hit[0]["rest"]:
                problems.append(f"не отмечено: {label}")
            used.update(i["ref"] for i in hit)
    for i in items:                                     # лишнее: заполнено, но в плане такого нет
        if i["ref"] in used or i["kind"] in ("checkbox", "radio", "button", "link", "file"):
            continue
        if i["value"]:                                  # предзаполненное сайтом тоже сюда: планировщик должен его учесть
            problems.append(f"лишнее значение не из плана: {i['ref']} {i['kind']} «{i['value'][:60]}»")
    return problems


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    d = Path(sys.argv[1])
    plan = json.loads((d / "fill-plan.json").read_text(encoding="utf-8"))
    text = Path(sys.argv[sys.argv.index("--snap") + 1]).read_text() if "--snap" in sys.argv else snap()
    (d / "verify-snap.txt").write_text(text, encoding="utf-8")
    problems = check(plan, parse(text))
    for p in problems:
        print(f"- {p}")
    print("VERIFY: ok" if not problems else "VERIFY: mismatch")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
