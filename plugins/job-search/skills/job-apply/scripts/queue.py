#!/usr/bin/env python3
"""Очередь откликов из Telegram-дайджеста (кнопка «✅ N» → digest.py queue).

  queue.py list [--all]            что ждёт (new, bl-review, paused) или всё
  queue.py next                    ключ первого `new`/`bl-review` (пусто — очередь пуста)
  queue.py show <key>              строка очереди как JSON
  queue.py set <key> <status> [--note …]
      status: new | in-progress | done | skipped | paused | failed | bl-review | bl-done

Файл: $JOB_APPLY_HOME/queue.tsv (date key status feed title href details post [note]).
"""
import argparse, json, os, re, sys
from pathlib import Path

F = Path(os.environ.get("JOB_APPLY_HOME", "~/.local/share/job-apply")).expanduser() / "queue.tsv"
FIELDS = ["date", "key", "status", "feed", "title", "href", "details", "post", "note"]
STATUSES = ["new", "in-progress", "done", "skipped", "paused", "failed", "bl-review", "bl-done"]
OPEN = ("new", "bl-review", "paused")


def load():
    if not F.exists():
        return []
    lines = F.read_text(encoding="utf-8").splitlines()[1:]
    return [dict(zip(FIELDS, l.split("\t") + [""] * len(FIELDS))) for l in lines if l.strip()]


def save(rows):
    tmp = F.with_suffix(".tmp")
    clean = lambda s: re.sub(r"[\t\n]+", " ", s or "")
    tmp.write_text("\t".join(FIELDS) + "\n" + "".join("\t".join(clean(r[k]) for k in FIELDS) + "\n" for r in rows),
                   encoding="utf-8")
    tmp.replace(F)


def find(rows, key):
    for r in rows:
        if r["key"] == key:
            return r
    sys.exit(f"ERR: нет {key} в {F}")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    l = sp.add_parser("list"); l.add_argument("--all", action="store_true")
    sp.add_parser("next")
    sp.add_parser("show").add_argument("key")
    s = sp.add_parser("set"); s.add_argument("key"); s.add_argument("status", choices=STATUSES); s.add_argument("--note")
    a = ap.parse_args()
    rows = load()
    if a.cmd == "list":
        for r in rows:
            if a.all or r["status"] in OPEN:
                print(f"{r['key']:>8}  {r['status']:<11} {r['title']}  {r['href'] or r['post']}"
                      + (f"  — {r['note']}" if r["note"] else ""))
    elif a.cmd == "next":
        r = next((r for r in rows if r["status"] in ("new", "bl-review")), None)
        if r:
            print(r["key"])
    elif a.cmd == "show":
        print(json.dumps(find(rows, a.key), ensure_ascii=False))
    else:
        r = find(rows, a.key)
        r["status"] = a.status
        if a.note is not None:
            r["note"] = a.note
        save(rows)
        print(f"{a.key}: {a.status}")


if __name__ == "__main__":
    main()
