#!/usr/bin/env python3
"""Outbox для job_tracker: строки, которые нужно записать в Google Sheets, когда это будет возможно.

Headless-прогон (claude -p) коннекторов claude.ai не видит. `add` сначала пишет в лист сам через
Apps Script (sheet.py, если настроен), иначе кладёт строку сюда, а интерактивная сессия переносит её
(или `sheet.py flush`, когда web app появится) и помечает done.

  tracker.py add [--outbox] < row.json   объект {"A": …, …, "N": …} или список из 14 значений
  tracker.py pending [--count]     что ждёт записи (JSON-строки с id)
  tracker.py done <id>...          записано в лист → убрать из outbox

Файл: $JOB_APPLY_HOME/tracker-outbox.jsonl
"""
import argparse, json, os, sys, time
from pathlib import Path

F = Path(os.environ.get("JOB_APPLY_HOME", "~/.local/share/job-apply")).expanduser() / "tracker-outbox.jsonl"
COLS = "ABCDEFGHIJKLMN"


def load():
    if not F.exists():
        return []
    return [json.loads(l) for l in F.read_text(encoding="utf-8").splitlines() if l.strip()]


def save(items):
    tmp = F.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
    tmp.replace(F)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    ad = sp.add_parser("add"); ad.add_argument("--outbox", action="store_true", help="не пробовать лист")
    p = sp.add_parser("pending"); p.add_argument("--count", action="store_true")
    d = sp.add_parser("done"); d.add_argument("ids", nargs="+")
    a = ap.parse_args()
    items = load()
    if a.cmd == "add":
        data = json.load(sys.stdin)
        row = [str(data.get(c, "")) for c in COLS] if isinstance(data, dict) else [str(x) for x in data]
        if len(row) != 14 or not all(row[:3]):
            sys.exit("ERR: нужны 14 колонок A–N, и A (компания), B (роль), C (дата) не пустые")
        if not a.outbox:
            sys.path.insert(0, str(Path(__file__).parent))
            import sheet
            if sheet.conf():
                try:
                    res = sheet.call("append", row=row)
                    print(f"job_tracker: {'уже есть' if res.get('duplicate') else 'записано'}, строка {res['row']}")
                    return
                except (Exception, SystemExit) as e:          # сеть/скрипт упали — не теряем строку
                    print(f"WARN: лист недоступен ({e}), кладу в outbox", file=sys.stderr)
        F.parent.mkdir(parents=True, exist_ok=True)
        item = {"id": time.strftime("%Y%m%d%H%M%S"), "row": row}
        save(items + [item])
        print(f"в outbox: {item['id']} ({row[0]} — {row[1]})")
    elif a.cmd == "pending":
        if a.count:
            print(len(items))
        else:
            for i in items:
                print(json.dumps(i, ensure_ascii=False))
    else:
        left = [i for i in items if i["id"] not in a.ids]
        save(left)
        print(f"записано: {len(items) - len(left)}, осталось: {len(left)}")


if __name__ == "__main__":
    main()
