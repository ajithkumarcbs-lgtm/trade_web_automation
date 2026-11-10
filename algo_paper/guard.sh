#!/bin/bash
# Every 2 minutes (cron): if the server is short of memory, the paper algo stops, so the live bot keeps its room. Once there is
# room again, and at least WAIT_MIN after that stop, the guard starts it again, so the week's test goes on (10 Nov it stayed
# stopped from 15:22). Only a stop the guard made is undone: one by hand stays. Logs to ~/algo/guard.log and tells you through
# the algo's own Telegram bot.
STOP_BELOW_MB=75
START_ABOVE_MB=250
WAIT_MIN=30
STATE=/home/ubuntu/algo/guard.stopped  # the time of the guard's last stop, until it starts the algo again
avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)

tell() {
  set -a; . /home/ubuntu/algo/.env; set +a
  curl -s -m 20 "https://api.telegram.org/bot${ALGO_BOT_TOKEN}/sendMessage" -d chat_id="${ALGO_CHAT_ID}" \
    --data-urlencode text="$1" > /dev/null
}

if [ "$avail" -lt "$STOP_BELOW_MB" ] && systemctl is-active --quiet algo-paper; then
  sudo systemctl stop algo-paper
  date +%s > "$STATE"
  echo "$(date '+%F %T') stopped algo-paper: MemAvailable ${avail} MB" >> /home/ubuntu/algo/guard.log
  tell "⚠️ Algo paper test stopped: the server had only ${avail} MB free. The live bot is untouched. The algo starts again by itself once there is room (${START_ABOVE_MB} MB, ${WAIT_MIN} min on)."
elif [ -f "$STATE" ] && [ "$avail" -ge "$START_ABOVE_MB" ] && ! systemctl is-active --quiet algo-paper \
     && [ $(( $(date +%s) - $(cat "$STATE") )) -ge $(( WAIT_MIN * 60 )) ]; then
  sudo systemctl start algo-paper
  rm -f "$STATE"
  echo "$(date '+%F %T') started algo-paper again: MemAvailable ${avail} MB" >> /home/ubuntu/algo/guard.log
  tell "✅ Algo paper test running again (${avail} MB free on the server)."
fi
