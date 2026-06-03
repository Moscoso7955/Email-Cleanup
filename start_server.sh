cd /workspaces/Email-Cleanup

# Kill any stale Flask or watchdog processes
pkill -f 'python app.py' 2>/dev/null || true
pkill -f 'keep_alive.sh' 2>/dev/null || true
sleep 1

# Start Flask in background
nohup python app.py >> flask.log 2>&1 &
echo "[$(date)] Flask started (PID $!)" >> flask.log

# Wait for Flask to be ready
for i in $(seq 1 15); do
  if curl -sf http://localhost:5000/ > /dev/null 2>&1; then
    echo "[$(date)] Flask ready after ${i}s" >> flask.log
    break
  fi
  sleep 1
done

# Set port 5000 to public so OAuth callbacks work
gh codespace ports visibility 5000:public -c "$CODESPACE_NAME" 2>/dev/null && \
  echo "[$(date)] Port 5000 set to public" >> flask.log || \
  echo "[$(date)] Warning: could not set port visibility (gh cli unavailable)" >> flask.log

# Start watchdog loop in background
nohup bash /workspaces/Email-Cleanup/keep_alive.sh >> flask.log 2>&1 &
echo "[$(date)] Watchdog started (PID $!)" >> flask.log
