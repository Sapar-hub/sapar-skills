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
# статусы откликов hh → трекер, сводка в Telegram (job-apply-hhstatus)
HH_ONCALENDAR=${HH_ONCALENDAR:-'*-*-* 10,15,20:43:00'}   # 3 раза в день: hh писем на Mail.ru не шлёт, mailwatch по письму hh почти не срабатывает
# письма о статусах (Mail.ru IMAP) → трекер, сводка в Telegram (job-apply-mailstatus)
MAIL_ONCALENDAR=${MAIL_ONCALENDAR:-'*-*-* 08,20:17:00'}   # подстраховка: основной путь — mailwatch (IMAP IDLE)
# напоминание в открытые чаты hh + автозакрытие после игнора (hhchat.py, autoclose.py)
FOLLOWUP_ONCALENDAR=${FOLLOWUP_ONCALENDAR:-'*-*-* 13:37:00'}

if [ "${1:-}" = uninstall ]; then
  systemctl --user disable --now job-apply-xvfb.service job-apply-worker.timer job-apply-topics.timer job-apply-hhstatus.timer job-apply-mailstatus.timer job-apply-followup.timer job-apply-mailwatch.service 2>/dev/null || true
  rm -f "$UNIT_DIR"/job-apply-xvfb.service "$UNIT_DIR"/job-apply-worker.{service,timer} "$UNIT_DIR"/job-apply-topics.{service,timer} "$UNIT_DIR"/job-apply-hhstatus.{service,timer} "$UNIT_DIR"/job-apply-mailstatus.{service,timer} "$UNIT_DIR"/job-apply-followup.{service,timer} "$UNIT_DIR"/job-apply-mailwatch.service
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
# креды Mail.ru для mailstatus.py (IMAP) — из текущего окружения, если их ещё нет в файле
for v in MAILRU_USER MAILRU_APPPASS; do
  { [ -n "${!v:-}" ] && ! grep -q "^$v=" "$ENVF" && printf '%s=%s\n' "$v" "${!v}" >> "$ENVF"; } || true
done
chmod 600 "$ENVF"
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
# невидимый дисплей для браузера таймеров (browser.mjs start --offscreen → DISPLAY=:99). Без него окно падает в headless,
# а если :99 пропадёт под живым окном, Chromium виснет (start это лечит, но дисплей у job-apply должен быть свой)
cat > "$UNIT_DIR/job-apply-xvfb.service" <<EOF
[Unit]
Description=job-apply: invisible X display :99 for the timer browser

[Service]
ExecStart=/usr/bin/Xvfb :99 -screen 0 1400x1000x24 -nolisten tcp
Restart=on-failure

[Install]
WantedBy=default.target
EOF
# статусы откликов на hh → трекер (hhstatus.py сам поднимает браузер; hh ходит мимо прокси — browser.mjs)
cat > "$UNIT_DIR/job-apply-hhstatus.service" <<EOF
[Unit]
Description=job-apply: hh.ru application statuses -> job_tracker

[Service]
Type=oneshot
EnvironmentFile=-$ENVF
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin
ExecStart=/usr/bin/env python3 -I $HERE/hhstatus.py --apply --tg
TimeoutStartSec=20min
EOF
cat > "$UNIT_DIR/job-apply-hhstatus.timer" <<EOF
[Unit]
Description=job-apply: hh statuses 3x a day

[Timer]
OnCalendar=$HH_ONCALENDAR
Persistent=true

[Install]
WantedBy=timers.target
EOF
# письма о статусах (Mail.ru IMAP, папки Отклики/INBOX/Newsletters) → трекер (mailstatus.py, без браузера)
cat > "$UNIT_DIR/job-apply-mailstatus.service" <<EOF
[Unit]
Description=job-apply: Mail.ru job letters -> job_tracker

[Service]
Type=oneshot
EnvironmentFile=-$ENVF
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin
ExecStart=/usr/bin/env python3 -I $HERE/mailstatus.py --apply --tg
TimeoutStartSec=15min
EOF
cat > "$UNIT_DIR/job-apply-mailstatus.timer" <<EOF
[Unit]
Description=job-apply: mail statuses backstop (mailwatch is the main path)

[Timer]
OnCalendar=$MAIL_ONCALENDAR
Persistent=true

[Install]
WantedBy=timers.target
EOF
# почта по IMAP IDLE: новое письмо → mailstatus сразу; письмо от hh (в т.ч. из «Спама») → и hhstatus
cat > "$UNIT_DIR/job-apply-mailwatch.service" <<EOF
[Unit]
Description=job-apply: Mail.ru IMAP IDLE -> mailstatus / hhstatus on new letters
After=network-online.target

[Service]
Type=simple
EnvironmentFile=-$ENVF
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin
ExecStart=/usr/bin/env python3 -I $HERE/mailwatch.py
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
EOF
cat > "$UNIT_DIR/job-apply-followup.service" <<EOF
[Unit]
Description=job-apply: hh chat reminders, then auto-close ignored applications

[Service]
Type=oneshot
EnvironmentFile=-$ENVF
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin
ExecStart=/usr/bin/env python3 -I $HERE/hhchat.py --apply --tg
ExecStart=/usr/bin/env python3 -I $HERE/autoclose.py --apply --tg
TimeoutStartSec=40min
EOF
cat > "$UNIT_DIR/job-apply-followup.timer" <<EOF
[Unit]
Description=job-apply: daily follow-up (hh chats) and auto-close

[Timer]
OnCalendar=$FOLLOWUP_ONCALENDAR
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
  "$TG" action add cv -- "$PY" -I "$HERE/board.py" cv {arg} >/dev/null          # deep link «резюме» в /tracker
fi
systemctl --user daemon-reload
systemctl --user enable --now job-apply-xvfb.service job-apply-worker.timer job-apply-topics.timer job-apply-hhstatus.timer job-apply-mailstatus.timer job-apply-followup.timer job-apply-mailwatch.service >/dev/null
echo "OK: job-apply-worker.service + timer ($ONCALENDAR), job-apply-topics.timer ($TOPICS_ONCALENDAR), job-apply-hhstatus.timer ($HH_ONCALENDAR), job-apply-mailstatus.timer ($MAIL_ONCALENDAR), job-apply-mailwatch.service (IMAP IDLE)${TG:+, команды /queue /tracker /contacts /cleanup}"
