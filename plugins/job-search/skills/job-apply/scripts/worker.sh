#!/usr/bin/env bash
# Разбирает очередь откликов (queue.tsv) по одному: claude -p + job-apply, общение через Telegram (tg).
#   worker.sh            всё, что new / bl-review; останавливается на первом paused (вкладка остаётся как есть)
#   worker.sh <key>      один конкретный элемент (в т.ч. paused — продолжить)
# Запускается кнопкой «✅ N» под постом дайджеста (digest.py queue → systemctl start job-apply-worker).
set -uo pipefail

SCRIPTS=$(cd "$(dirname "$0")" && pwd)
SKILL=$(dirname "$SCRIPTS")
H=${JOB_APPLY_HOME:-$HOME/.local/share/job-apply}
CONF=${JOB_APPLY_CONFIG:-$HOME/.config/job-apply}
export PATH="$HOME/.local/bin:$PATH"
# прокси и прочее окружение интерактивного шелла (под systemd его нет: claude из РФ без прокси = 403)
if [ -f "$CONF/worker.env" ]; then set -a; . "$CONF/worker.env"; set +a; fi
TG=$(command -v tg) || { echo "ERR: нет tg (плагин tg-bridge)"; exit 1; }
CLAUDE=${JOB_APPLY_AGENT:-$(command -v claude)} || { echo "ERR: нет claude"; exit 1; }
Q="python3 -I $SCRIPTS/queue.py"

mkdir -p "$H/worker-logs"
exec 9>"$H/worker.lock"
flock -n 9 || { echo "worker уже работает"; exit 0; }

# браузер с логинами пользователя: без него отклика нет
if ! curl -s -m 3 http://127.0.0.1:9333/json/version >/dev/null; then
  (cd "$SCRIPTS" && node browser.mjs start) >/dev/null 2>&1 &
  sleep 8
  curl -s -m 3 http://127.0.0.1:9333/json/version >/dev/null || {
    "$TG" send "job-apply worker: браузер не стартовал (CDP 9333). Очередь ждёт: queue.py list"; exit 1; }
fi

ALLOWED=(
  "Bash(node:*)" "Bash(python3:*)" "Bash(tg:*)" "Bash(pdftoppm:*)" "Bash(cloudflared:*)"
  "Bash(ls:*)" "Bash(cat:*)" "Bash(head:*)" "Bash(grep:*)" "Bash(mkdir:*)" "Bash(cp:*)" "Bash(date:*)"
  "Read" "Write" "Edit" "Glob" "Grep" "Skill" "WebSearch" "WebFetch"
  "mcp__claude_ai_Google_Sheets__get_values" "mcp__claude_ai_Google_Sheets__append_values"
  "mcp__claude_ai_Google_Sheets__update_values" "mcp__claude_ai_Google_Sheets__get_spreadsheet"
)

run_one() {
  local key=$1 log status line
  log="$H/worker-logs/$(date +%Y%m%d-%H%M%S)-$key.log"
  $Q set "$key" in-progress >/dev/null
  cd "$SKILL"
  "$CLAUDE" -p "Скилл job-apply, headless-режим. Сначала прочитай references/auto-apply.md и SKILL.md, \
затем обработай элемент очереди $key (python3 -I scripts/queue.py show $key). \
Пользователь только в Telegram (tg). Последняя строка ответа — STATUS: …" \
    --permission-mode acceptEdits --allowedTools "${ALLOWED[@]}" \
    --add-dir "$H" --add-dir "$CONF" >"$log" 2>&1 </dev/null
  line=$(grep -E '^STATUS: (done|skipped|paused|failed|bl-done)' "$log" | tail -1)
  status=$(printf '%s' "$line" | awk '{print $2}')
  [ -n "$status" ] || { status=failed; line="STATUS: failed — агент не вернул статус"; }
  $Q set "$key" "$status" --note "${line#STATUS: $status}" >/dev/null
  [ "$status" = failed ] && "$TG" send "job-apply worker: $key — $line
лог: $log"
  echo "$key $status"
  [ "$status" != paused ]                         # paused: дальше не идём, вкладку не трогаем
}

if [ $# -gt 0 ]; then
  run_one "$1"
  exit 0
fi
while key=$($Q next) && [ -n "$key" ]; do
  run_one "$key" || break
done
