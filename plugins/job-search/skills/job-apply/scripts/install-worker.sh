#!/usr/bin/env bash
# Ставит systemd --user юнит job-apply-worker (oneshot) + страховочный таймер раз в день.
#   ./install-worker.sh            ONCALENDAR='*-*-* 10:30' по умолчанию
#   ./install-worker.sh uninstall
# Ещё: таймер job-apply-topics (каждый день, TOPICS_ONCALENDAR) удаляет вкладки бота по закрытым вакансиям,
# и команды бота /queue, /cleanup (+ кнопка подтверждения topics-clean).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
UNIT_DIR=$HOME/.config/systemd/user
ONCALENDAR=${ONCALENDAR:-'*-*-* 10:30'}
TOPICS_ONCALENDAR=${TOPICS_ONCALENDAR:-'*-*-* 21:47'}

if [ "${1:-}" = uninstall ]; then
  systemctl --user disable --now job-apply-worker.timer job-apply-topics.timer 2>/dev/null || true
  rm -f "$UNIT_DIR"/job-apply-worker.{service,timer} "$UNIT_DIR"/job-apply-topics.{service,timer}
  systemctl --user daemon-reload
  echo "OK: удалено"; exit 0
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/job-apply-worker.service" <<EOF
[Unit]
Description=job-apply worker: queue from the Telegram digest -> applications (claude -p, gates in Telegram)

[Service]
Type=oneshot
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin
ExecStart=/usr/bin/env bash $HERE/worker.sh
TimeoutStartSec=3h
EOF
cat > "$UNIT_DIR/job-apply-worker.timer" <<EOF
[Unit]
Description=job-apply worker backstop

[Timer]
OnCalendar=$ONCALENDAR
Persistent=false

[Install]
WantedBy=timers.target
EOF
# прокси из текущего окружения → worker.env (systemd --user не видит переменные шелла)
ENVF=$HOME/.config/job-apply/worker.env
if [ ! -e "$ENVF" ]; then
  : > "$ENVF"; chmod 600 "$ENVF"
  for v in HTTPS_PROXY https_proxy HTTP_PROXY http_proxy ALL_PROXY all_proxy NO_PROXY no_proxy; do
    [ -n "${!v:-}" ] && printf '%s=%s\n' "$v" "${!v}" >> "$ENVF"
  done
  grep -q -i '^no_proxy=' "$ENVF" || printf 'NO_PROXY=localhost,127.0.0.1,::1\nno_proxy=localhost,127.0.0.1,::1\n' >> "$ENVF"
  echo "OK: $ENVF ($(grep -c . "$ENVF") переменных)"
fi
[ -e "$HOME/.config/job-apply/auto.yaml" ] || printf '# Сайты, где финальный submit без вопроса в Telegram (скрин и PDF всё равно придут).\n# Пример: no_confirm: [job.mts.ru]\nno_confirm: []\n' > "$HOME/.config/job-apply/auto.yaml"
# вкладки бота (tg-bridge): закрытая в трекере вакансия -> вкладка удаляется (topics.py clean)
cat > "$UNIT_DIR/job-apply-topics.service" <<EOF
[Unit]
Description=job-apply: delete Telegram topics of closed applications

[Service]
Type=oneshot
EnvironmentFile=-$ENVF
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin
ExecStart=/usr/bin/env python3 -I $HERE/topics.py clean
EOF
cat > "$UNIT_DIR/job-apply-topics.timer" <<EOF
[Unit]
Description=job-apply: daily topic cleanup

[Timer]
OnCalendar=$TOPICS_ONCALENDAR
Persistent=true

[Install]
WantedBy=timers.target
EOF
TG=$(command -v tg || { [ -x "$HOME/.local/bin/tg" ] && echo "$HOME/.local/bin/tg"; } || true)
if [ -n "$TG" ]; then
  PY=$(command -v python3)
  "$TG" action add queue --desc "очередь автооткликов: что ждёт, что упало и почему" -- "$PY" -I "$HERE/board.py" queue --tg >/dev/null
  "$TG" action add tracker --desc "трекер: открытые отклики карточками, у кого пора делать шаг" -- "$PY" -I "$HERE/board.py" tracker --tg >/dev/null
  "$TG" action add contacts --desc "контакты рекрутеров: кому писал, кто ответил" -- "$PY" -I "$HERE/board.py" contacts --tg >/dev/null
  "$TG" action add cleanup --desc "удалить все вкладки, кроме открытых в трекере (сначала покажет список)" -- "$PY" -I "$HERE/topics.py" cleanup >/dev/null
  "$TG" action add topics-clean -- "$PY" -I "$HERE/topics.py" clean-go {arg} >/dev/null
fi
systemctl --user daemon-reload
systemctl --user enable --now job-apply-worker.timer job-apply-topics.timer >/dev/null
echo "OK: job-apply-worker.service + timer ($ONCALENDAR), job-apply-topics.timer ($TOPICS_ONCALENDAR)${TG:+, команды /queue /tracker /contacts /cleanup}"
