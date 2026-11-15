---
name: event-digest
description: Use this skill when the user asks about IT/ML meetups and conferences to attend — «какие митапы на неделе», «куда сходить», «добавь канал с мероприятиями», «что я записал на “Иду”», «как прошёл митап», «покажи разборы походов» — or wants the event digest, the «Иду» button, the goal-the-day-before ping or the debrief ping set up, changed or debugged. It pulls Telegram channels and RSS (Habr «Конференции») through the job-digest RSSHub, keeps Moscow offline events, sends each to Telegram with «Иду / Скип», then asks for a one-line micro-goal the evening before and a debrief an hour after the end.
---

# event-digest

The point is going, not reading: a digest item ends in «Иду», then two questions in Telegram.

Pipeline: `feeds.txt` → RSSHub (shared with job-digest) → `scripts/events.py run` (post-level filter,
date parsing, dedup in sqlite) → `tg send` with «Иду / Скип» → `events.py tick` every 5 min:
- «Иду» with a parsed day and time → `going`; without a time or a day → the bot asks «Когда?» (two tries).
- The day before at 20:00 (or right away if «Иду» is pressed later) → «Микроцель — одна строка».
- An hour after the end (start + 3 h when the end is unknown) → `где было хуже всего → тело → слово → что сделал`.
- «не иду» / «не был» in any answer → `cancelled`. Silence → the stage is skipped (`no_report` after the event).
- One open question at a time: tg-bridge routes a plain (non-reply) text to the only open ask, so a second one waits for the next tick.

Files:
- code: `scripts/events.py` (imports `../job-digest/scripts/digest.py` for feeds, text and the db)
- config: `~/.config/event-digest/feeds.txt` (`<url> [link]`), `keywords.txt`
- state: `~/.local/state/event-digest/items.db` — `items` (as in job-digest) + `events`
  (`id, start, end, start_is_guess, end_is_guess, status, goal, report, asked, ask_pid`)

## Answering

```bash
python3 -I scripts/events.py show --days 1     # upcoming events with status (new/going/need_date/…)
python3 -I scripts/events.py show --days 7 --all   # everything fetched, + passed / - filtered out
python3 -I scripts/events.py log               # goals and debriefs of past events
# in the bot: /events (= show --days 1) and /eventlog (= log)
python3 -I scripts/events.py parse "20 ноября, 19:00–22:00"   # check date parsing
```

For a debrief question («как я сходил», «что повторяется»), read `log` and point at repeats in
«где было хуже всего / тело / слово» across events; do not judge, do not invent entries.

## Filter

Whole post, not per line (an announcement is one event): a topic regex (plain line in `keywords.txt`) and a
place regex (`+москв`) must hit, no `!exclusion` may hit (вебинар, марафон, курс, промокод, записи докладов),
and the parsed start must be in the future. A post without a day passes if it is fresh (≤14 days) — «Иду» asks for the date.
`link` after a feed URL sends posts without buttons (roundups with many events in one post).
Speakers and companies from the post are fair game as a reason to talk to someone; do not collect attendee profiles.

## Adding a source

Telegram: `http://127.0.0.1:1200/telegram/channel/<username>` (public: `t.me/s/<username>` opens without login).
RSSHub returning 200 does not mean it is the right channel — check the title:
`curl -s <url> | grep -m1 '<title>'`. Then `events.py run --dry-run`.

## Install / debug

```bash
scripts/install.sh                      # config, tg actions event-go/-skip, timers
systemctl --user list-timers 'event-*'
journalctl --user -u event-digest -u event-tick -n 50
```

Tests without the real bot: `EVENT_DIGEST_TG=/path/to/fake-tg` plus separate `EVENT_DIGEST_CONFIG` / `EVENT_DIGEST_STATE`.
A tg-bridge restart kills an open «Когда?» question; the next tick puts the event back to `new` so «Иду» works again.
