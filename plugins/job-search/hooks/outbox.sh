#!/bin/sh
# SessionStart: once per session, remind about tracker rows that headless runs could not write (count only).
F=${JOB_APPLY_HOME:-$HOME/.local/share/job-apply}/tracker-outbox.jsonl
if [ -s "$F" ]; then
  n=$(grep -c . "$F")
  echo "job-apply: в tracker-outbox $n строк для job_tracker (после автоотклика). Перенести: job-apply SKILL.md, раздел «Tracker outbox»."
fi
exit 0
