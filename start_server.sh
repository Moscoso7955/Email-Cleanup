#!/bin/bash
# Runs on every Codespace start/resume
cd /workspaces/Email-Cleanup

# Kill any stale Flask or watchdog processes
pkill -f 'python app.py' 2>/dev/null || true
pkill -f 'keep_alive.sh' 2>/dev/null || true
sleep 1

# Start Flask in background
nohup python app.py >> flask.log 2>&1 &
echo "[$(date)] Flask started (PID $!)" >> flask.log

# Start watchdog loop in background
nohup bash /workspaces/Email-Cleanup/keep_alive.sh >> flask.log 2>&1 &
echo "[$(date)] Watchdog started (PID $!)" >> flask.log
