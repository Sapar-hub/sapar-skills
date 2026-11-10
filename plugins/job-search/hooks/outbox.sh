#!/bin/sh
# UserPromptSubmit: напомнить агенту, что headless-прогоны оставили строки для job_tracker.
F=${JOB_APPLY_HOME:-$HOME/.local/share/job-apply}/tracker-outbox.jsonl
[ -s "$F" ] || exit 0
n=$(grep -c . "$F")
echo "job-apply: в tracker-outbox $n строк для job_tracker (после автоотклика). Перенести: job-apply SKILL.md, раздел «Tracker outbox»."
exit 0
