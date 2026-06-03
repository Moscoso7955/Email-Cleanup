while true; do
  if ! curl -sf http://localhost:5000/ > /dev/null 2>&1; then
    echo "[Wed Jun  3 23:33:25 UTC 2026] Flask not responding - restarting..." >> flask.log
    pkill -f 'python app.py' 2>/dev/null
    fuser -k 5000/tcp 2>/dev/null
    sleep 2
    nohup python app.py >> flask.log 2>&1 &
    echo "[Wed Jun  3 23:33:25 UTC 2026] Flask restarted (PID 4031)" >> flask.log
    sleep 10
    gh codespace ports visibility 5000:public -c "fantastic-space-disco-v6xx96g9grqgh6p5g" 2>/dev/null || true
  fi
  gh codespace list --json name -q '.[0].name' > /dev/null 2>&1 || true
  sleep 5
done
