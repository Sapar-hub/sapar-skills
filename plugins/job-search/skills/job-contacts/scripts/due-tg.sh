#!/bin/sh
# Ежедневный follow-up в Telegram (tg-bridge): что вывел `contacts.py due`, без шапки; пусто — ничего не шлём.
HERE=$(cd "$(dirname "$0")" && pwd)
TG=$(command -v tg || echo "$HOME/.local/bin/tg")
[ -x "$TG" ] || exit 0
out=$(/usr/bin/env python3 -I "$HERE/contacts.py" due "$@" | grep -v '^#')
[ -n "$out" ] || exit 0
n=$(printf '%s\n' "$out" | wc -l)
printf 'Follow-up на сегодня (%s):\n\n%s\n\nРазобрать: «что с откликами» в сессии агента (job-contacts).\n' "$n" "$out" \
  | "$TG" send --stdin
