#!/bin/sh
# Azure App Service startup command: "sh startup.sh".
# If app.py exits (say Supabase was unreachable for the whole startup retry window), App Service does
# not restart it: it falls back to its own placeholder site and only replaces the instance after an
# hour of failed health checks. So restart it here, and pass a stop signal on so saves can finish.
child=
trap 'kill -TERM "$child" 2>/dev/null; wait "$child"; exit 0' TERM INT
while true; do
  python3 app.py &
  child=$!
  wait "$child"
  echo "lifeline: app.py exited with status $?; restarting in 10 s" >&2
  sleep 10
done
