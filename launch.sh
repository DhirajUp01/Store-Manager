#!/bin/bash
#
# TP-IMS App Launcher
# Checks if server is running on port 5050, starts it if not, then opens browser.
#

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
FLASK_APP="$APP_DIR/app.py"
PORT=5050
BROWSER="open"   # macOS default browser

echo "[TP-IMS] Starting Store Manager..."

# Check if port 5050 is already listening
if nc -z localhost $PORT 2>/dev/null; then
    echo "[TP-IMS] Server already running on port $PORT"
else
    echo "[TP-IMS] Server not running. Starting app.py..."
    cd "$APP_DIR"
    nohup python3 "$FLASK_APP" >> "$APP_DIR/server.log" 2>&1 &
    SERVER_PID=$!
    echo "[TP-IMS] Server started with PID $SERVER_PID"

    # Wait up to 10 seconds for server to start
    for i in $(seq 1 10); do
        sleep 1
        if nc -z localhost $PORT 2>/dev/null; then
            echo "[TP-IMS] Server ready on port $PORT"
            break
        fi
        echo "[TP-IMS] Waiting for server... ($i/10)"
    done
fi

# Open browser
echo "[TP-IMS] Opening http://localhost:$PORT"
$BROWSER "http://localhost:$PORT"
