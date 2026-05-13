#!/bin/bash
cd /workspaces/Email-Cleanup
while true; do
  if ! pgrep -f 'python app.py' > /dev/null; then
    echo "[$(date)] Flask not running -- restarting..." >> flask.log
    nohup python app.py >> flask.log 2>&1 &
    sleep 3
  fi
  sleep 5
done
