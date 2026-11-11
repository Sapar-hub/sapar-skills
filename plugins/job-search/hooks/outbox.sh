#!/bin/sh
# UserPromptSubmit: напомнить агенту про хвосты job-apply (только счётчики, без содержимого).
F=${JOB_APPLY_HOME:-$HOME/.local/share/job-apply}/tracker-outbox.jsonl
if [ -s "$F" ]; then
  n=$(grep -c . "$F")
  echo "job-apply: в tracker-outbox $n строк для job_tracker (после автоотклика). Перенести: job-apply SKILL.md, раздел «Tracker outbox»."
fi
S="${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}/skills/job-apply/scripts/status.py"
[ -f "$S" ] && timeout 3 python3 -I "$S" --hook 2>/dev/null
exit 0
