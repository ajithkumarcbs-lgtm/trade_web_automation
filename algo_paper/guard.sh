#!/bin/bash
# Every 2 minutes (cron): if the server is short of memory, the paper algo stops, so the live bot keeps its room.
# Logs to ~/algo/guard.log and tells you through the algo's own Telegram bot.
LIMIT_MB=60
avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
if [ "$avail" -lt "$LIMIT_MB" ] && systemctl is-active --quiet algo-paper; then
  sudo systemctl stop algo-paper
  echo "$(date '+%F %T') stopped algo-paper: MemAvailable ${avail} MB" >> /home/ubuntu/algo/guard.log
  set -a; . /home/ubuntu/algo/.env; set +a
  curl -s -m 20 "https://api.telegram.org/bot${ALGO_BOT_TOKEN}/sendMessage" -d chat_id="${ALGO_CHAT_ID}" \
    --data-urlencode text="⚠️ Algo paper test stopped: the server had only ${avail} MB free. The live bot is untouched." > /dev/null
fi
