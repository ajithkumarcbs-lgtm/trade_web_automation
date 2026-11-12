#!/bin/bash
# Once a day at a quiet hour (cron every 10 min, 04:00-06:59 IST): restarts the live bot when nothing is in flight, to give
# back the memory it grows. 10-12 Nov it went from 328 MB after a restart to 782 MB 35 hours later, 551 MB of that in swap
# on a 911 MB server. On 11 Nov page reads then timed out 100+ times an hour, and #130 Kasnikowski went 3 hours without a
# price, so no auto-bookset. A restart takes ~30 s; posts in that time are missed, hence the quiet hour and these checks.
cd /home/ubuntu/tradebot || exit 1
LOG=/home/ubuntu/tradebot_refresh.log
STAMP=/home/ubuntu/tradebot_refresh.day
MIN_MB=450  # below this (RAM + swap of the bot), no restart is needed
[ "$(cat "$STAMP" 2>/dev/null)" = "$(date +%F)" ] && exit 0

used() {
  local f=/sys/fs/cgroup/system.slice/tradebot.service
  echo $(( ($(grep '^anon ' $f/memory.stat | cut -d' ' -f2) + $(cat $f/memory.swap.current)) / 1048576 ))
}
before=$(used)
if [ "$before" -lt "$MIN_MB" ]; then
  echo "$(date '+%F %T') not needed: ${before} MB" >> "$LOG"; date +%F > "$STAMP"; exit 0
fi
busy=$(.venv/bin/python - <<'P'
import sqlite3
from datetime import datetime, timedelta
c = sqlite3.connect("data/trades.db")
now = datetime.now()
at = lambda delta: (now - delta).isoformat(timespec="seconds")
why = []
if c.execute("SELECT COUNT(*) FROM pending").fetchone()[0]:
    why.append("entries waiting")
if c.execute("SELECT COUNT(*) FROM positions WHERE mode='live' AND closed=0 AND close_pending_since IS NOT NULL").fetchone()[0]:
    why.append("a booking being retried")
if c.execute("SELECT COUNT(*) FROM ladders WHERE mode='live' AND ended_at IS NULL AND start_at <= ?", (at(timedelta()),)).fetchone()[0]:
    why.append("an SK-method match on")
if c.execute("SELECT COUNT(*) FROM positions WHERE mode='live' AND closed=0 AND opened_at >= ?", (at(timedelta(hours=12)),)).fetchone()[0]:
    why.append("a trade opened in the last 12 h")
if c.execute("SELECT COUNT(*) FROM messages WHERE received_at >= ?", (at(timedelta(minutes=15)),)).fetchone()[0]:
    why.append("posts in the last 15 min")
print(", ".join(why))
P
)
if [ -n "$busy" ]; then
  echo "$(date '+%F %T') not now (${before} MB): $busy" >> "$LOG"; exit 0
fi
sudo systemctl restart tradebot
date +%F > "$STAMP"
sleep 45
echo "$(date '+%F %T') restarted: ${before} MB before, $(used) MB after, service $(systemctl is-active tradebot)" >> "$LOG"
