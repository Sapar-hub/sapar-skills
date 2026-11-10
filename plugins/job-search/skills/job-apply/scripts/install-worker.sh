#!/usr/bin/env bash
# Ставит systemd --user юнит job-apply-worker (oneshot) + страховочный таймер раз в день.
#   ./install-worker.sh            ONCALENDAR='*-*-* 10:30' по умолчанию
#   ./install-worker.sh uninstall
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
UNIT_DIR=$HOME/.config/systemd/user
ONCALENDAR=${ONCALENDAR:-'*-*-* 10:30'}

if [ "${1:-}" = uninstall ]; then
  systemctl --user disable --now job-apply-worker.timer 2>/dev/null || true
  rm -f "$UNIT_DIR"/job-apply-worker.{service,timer}
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
systemctl --user daemon-reload
systemctl --user enable --now job-apply-worker.timer >/dev/null
echo "OK: job-apply-worker.service + timer ($ONCALENDAR)"
