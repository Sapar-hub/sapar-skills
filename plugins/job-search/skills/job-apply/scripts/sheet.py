#!/usr/bin/env python3
"""job_tracker через Apps Script web app (apps-script/tracker.gs) — работает и там, где нет коннекторов Google
(claude -p, задачи из Telegram).

  sheet.py ping
  sheet.py find <слова…> [--limit N]          строки, где все слова есть в A (компания), B (роль) или I (ссылка)
  sheet.py append [--force] < row.json        {"A": …, "N": …} или список из 14; дубль A+B+C не пишет
  sheet.py update <row> [--set N=текст]... [--add N=строка]...   --add дописывает строкой в конец ячейки
  sheet.py flush                              outbox (tracker.py) → лист; записанное убирается из outbox

Конфиг: ~/.config/job-apply/sheet.env — TRACKER_URL, TRACKER_SECRET (см. apps-script/tracker.gs).
Код выхода 3 = не настроено (используй tracker.py add — outbox).
"""
import argparse, json, os, sys, urllib.request
from pathlib import Path

ENV = Path(os.environ.get("JOB_APPLY_CONFIG", "~/.config/job-apply")).expanduser() / "sheet.env"
COLS = "ABCDEFGHIJKLMN"


def conf():
    c = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                c[k.strip()] = v.strip().strip("'\"")
    return c if c.get("TRACKER_URL") and c.get("TRACKER_SECRET") else None


def call(op, **kw):
    c = conf()
    if not c:
        print(f"не настроено: нет {ENV} (TRACKER_URL, TRACKER_SECRET)", file=sys.stderr)
        sys.exit(3)
    body = json.dumps({"secret": c["TRACKER_SECRET"], "op": op, **kw}, ensure_ascii=False).encode()
    req = urllib.request.Request(c["TRACKER_URL"], data=body, headers={"Content-Type": "application/json"})
    # Apps Script отвечает 302 на googleusercontent; urllib идёт туда GET'ом — там и лежит ответ
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.loads(r.read())
    if not res.get("ok"):
        sys.exit(f"ERR: {res.get('error')}")
    return res


def to_row(data):
    row = [str(data.get(c, "")) for c in COLS] if isinstance(data, dict) else [str(x) for x in data]
    if len(row) != 14 or not all(row[:3]):
        sys.exit("ERR: нужны 14 колонок A–N, и A, B, C не пустые")
    return row


def kv(items):
    out = {}
    for it in items:
        k, _, v = it.partition("=")
        if k not in COLS or len(k) != 1:
            sys.exit(f"ERR: колонка A–N, а не «{k}»")
        out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("ping")
    f = sp.add_parser("find"); f.add_argument("words", nargs="*"); f.add_argument("--limit", type=int, default=20)
    a = sp.add_parser("append"); a.add_argument("--force", action="store_true")
    u = sp.add_parser("update"); u.add_argument("row", type=int)
    u.add_argument("--set", action="append", default=[]); u.add_argument("--add", action="append", default=[])
    sp.add_parser("flush")
    a = ap.parse_args()
    if a.cmd == "ping":
        print(f"ok, последняя строка: {call('ping')['last_row']}")
    elif a.cmd == "find":
        for r in call("find", query=" ".join(a.words), limit=a.limit)["rows"]:
            v = r["values"]
            print(f"#{r['row']}  {v[0]} — {v[1]} | {v[2]} | {v[3]} / {v[4]} | след.: {v[5]}")
    elif a.cmd == "append":
        res = call("append", row=to_row(json.load(sys.stdin)), force=a.force)
        print(f"{'уже есть' if res.get('duplicate') else 'записано'}: строка {res['row']}")
    elif a.cmd == "update":
        res = call("update", row_number=a.row, set=kv(a.set), append=kv(a.add))
        print(f"строка {res['row']}: {res['values'][0]} — {res['values'][1]}")
    else:
        sys.path.insert(0, str(Path(__file__).parent))
        import tracker
        items = tracker.load()
        done = []
        for it in items:
            res = call("append", row=it["row"])
            done.append(it["id"])
            print(f"{it['row'][0]} — {it['row'][1]}: {'уже есть' if res.get('duplicate') else 'записано'}, строка {res['row']}")
        tracker.save([i for i in items if i["id"] not in done])


if __name__ == "__main__":
    main()
