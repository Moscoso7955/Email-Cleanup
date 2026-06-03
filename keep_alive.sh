#!/bin/bash
cd /workspaces/Email-Cleanup
while true; do
  if ! curl -sf http://localhost:5000/ > /dev/null 2>&1; then
    echo "[$(date)] Flask not responding - restarting..." >> flask.log
    pkill -f 'python app.py' 2>/dev/null
    fuser -k 5000/tcp 2>/dev/null
    sleep 2
    nohup python app.py >> flask.log 2>&1 &
    echo "[$(date)] Flask restarted (PID $!)" >> flask.log
    sleep 10
  fi
  # Keep codespace alive by doing gh CLI activity every 4 minutes
  gh codespace list --json name -q '.[0].name' > /dev/null 2>&1 || true
  sleep 5
done
