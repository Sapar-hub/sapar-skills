#!/usr/bin/env python3
"""hh.ru как второй канал: та же вакансия, на которую откликнулись на сайте работодателя, — откликнуться и на hh.

  second.py after <appdir> <key>   после отклика на сайте (pipeline.py): найти на hh → в очередь «<key>.hh»
  second.py scan [--dry]           по открытым строкам трекера без hh: найти на hh → в очередь «t<строка>.hh»

Поиск — `hh.mjs find --title --company` (страница поиска hh в залогиненном браузере; открытый API hh закрыт).
Найденное с пометкой «Вы откликнулись» не ставится. Элемент очереди с details «второй канал hh» pipeline.py
проводит без разведки: сразу форма отклика hh → план → заполнение → подтверждение.
"""
import argparse, json, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import status as S  # noqa: E402

MARK = "второй канал hh"


def hh_find(title, company):
    """-> (status, url, hh_title, employer) лучшего совпадения или None."""
    p = subprocess.run(["node", str(HERE / "hh.mjs"), "find", "--title", title, "--company", company],
                       capture_output=True, text=True, timeout=180)
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if parts[0] in ("FOUND", "APPLIED") and len(parts) >= 4:
            return parts[0], parts[1], parts[2], parts[3]
    if p.returncode:
        print(f"WARN hh find «{title}» / {company}: {p.stderr.strip()[-200:]}", file=sys.stderr)
    return None


def enqueue(key, title, company, url, note):
    p = subprocess.run([sys.executable, "-I", str(HERE / "queue.py"), "add", "--key", key, "--href", url,
                        "--title", f"hh: {title} — {company}", "--feed", "hh (второй канал)",
                        "--details", f"{MARK}: {company} | {title}", "--note", note],
                       capture_output=True, text=True)
    return p.stdout.strip() or p.stderr.strip()


def cmd_after(a):
    plan = json.loads((Path(a.appdir) / "fill-plan.json").read_text(encoding="utf-8"))
    if "hh.ru" in plan.get("url", ""):
        return                                                    # уже hh
    hit = hh_find(plan.get("title", ""), plan.get("company", ""))
    if not hit:
        print("hh: вакансии нет")
    elif hit[0] == "APPLIED":
        print(f"hh: уже откликались {hit[1]}")
    else:
        print("hh: " + enqueue(f"{a.key}.hh", plan["title"], plan["company"], hit[1], f"сайт: {plan.get('url', '')}"))


def cmd_scan(a):
    rows = S.fetch_sheet() or S.cached_sheet()[0] or []
    applied_hh = S.HOME / "applied.tsv"
    hh_lines = applied_hh.read_text(encoding="utf-8") if applied_hh.exists() else ""
    for r in rows:
        v = r["values"] + [""] * 14
        company, title, status, notes, url = v[0], v[1], v[4], v[7], v[8]
        if not company or not title or status in ("", "Закрыто") or "hh.ru" in url:
            continue
        if re.search(r"hh\.ru|\(hh\)", notes) or f"{company}\t{title}" in hh_lines and "(hh)" in hh_lines:
            continue
        hit = hh_find(title, company)
        tag = f"#{r['row']} {company} — {title[:50]}"
        if not hit:
            print(f"нет на hh: {tag}")
        elif hit[0] == "APPLIED":
            print(f"уже откликались на hh: {tag}")
        elif a.dry:
            print(f"нашёл: {tag} → {hit[1]} ({hit[2]} / {hit[3]})")
        else:
            print(f"в очередь: {tag} → {hit[1]}: " + enqueue(f"t{r['row']}.hh", title, company, hit[1], f"трекер #{r['row']}"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    af = sp.add_parser("after"); af.add_argument("appdir"); af.add_argument("key"); af.set_defaults(f=cmd_after)
    sc = sp.add_parser("scan"); sc.add_argument("--dry", action="store_true"); sc.set_defaults(f=cmd_scan)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
