#!/usr/bin/env bash
# Ставит event-digest: конфиг, tg actions «Иду/Скип», два systemd --user timer:
#   event-digest — забрать ленты раз в день; event-tick — раз в 5 мин пинги накануне/после.
#   ./install.sh
#   ONCALENDAR='*-*-* 10,18:17' ./install.sh
#   ./install.sh uninstall
# RSSHub общий с job-digest (его ставит job-digest/scripts/install.sh).
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
CONF=${EVENT_DIGEST_CONFIG:-$HOME/.config/event-digest}
UNIT_DIR=$HOME/.config/systemd/user
ONCALENDAR=${ONCALENDAR:-'*-*-* 10:17'}
PY=$(command -v python3)
E="$HERE/scripts/events.py"

if [ "${1:-}" = uninstall ]; then
  systemctl --user disable --now event-digest.timer event-tick.timer 2>/dev/null || true
  rm -f "$UNIT_DIR"/event-{digest,tick}.{service,timer}
  systemctl --user daemon-reload
  echo "OK: удалено (конфиг $CONF и база ~/.local/state/event-digest оставлены)"
  exit 0
fi

TG=$(command -v tg || { [ -x "$HOME/.local/bin/tg" ] && echo "$HOME/.local/bin/tg"; } || true)
[ -n "$TG" ] || { echo "ERR: нужен tg (плагин tg-bridge): без него некуда слать «Иду»"; exit 1; }
docker ps --format '{{.Names}}' 2>/dev/null | grep -qx rsshub \
  || echo "WARN: rsshub не запущен — поставь job-digest (scripts/install.sh) или подними контейнер"

mkdir -p "$CONF"
for f in feeds.txt keywords.txt; do
  [ -e "$CONF/$f" ] || cp "$HERE/defaults/$f" "$CONF/$f"
done

"$TG" action add event-go   -- "$PY" -I "$E" go {arg} >/dev/null
"$TG" action add event-skip -- "$PY" -I "$E" skip {arg} >/dev/null
# action без {arg} = команда в боте: /events — ближайшие и статусы, /eventlog — цели и разборы
"$TG" action add events   -- "$PY" -I "$E" show --days 1 >/dev/null
"$TG" action add eventlog -- "$PY" -I "$E" log >/dev/null

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/event-digest.service" <<EOF
[Unit]
Description=Event digest: meetups RSS -> Telegram
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=$PY -I $E run
EOF
cat > "$UNIT_DIR/event-digest.timer" <<EOF
[Unit]
Description=Event digest schedule

[Timer]
OnCalendar=$ONCALENDAR
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
EOF
# KillMode=process: tick завершается сразу, а вопрос в tg (дочерний процесс) живёт до ответа
cat > "$UNIT_DIR/event-tick.service" <<EOF
[Unit]
Description=Event pings: goal the day before, debrief after

[Service]
Type=oneshot
KillMode=process
ExecStart=$PY -I $E tick
EOF
cat > "$UNIT_DIR/event-tick.timer" <<EOF
[Unit]
Description=Event pings every 5 min

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now event-digest.timer event-tick.timer >/dev/null
echo "OK: actions event-go/event-skip, timers event-digest ($ONCALENDAR) и event-tick (каждые 5 мин)"
systemctl --user list-timers 'event-*' --no-pager | sed -n '2,3p'
