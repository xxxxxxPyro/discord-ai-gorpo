#!/usr/bin/env bash
set -euo pipefail

# Gorpo bot runner script
# Usage: run_bot.sh [start|stop|restart|status]

PROJECT_ROOT="/mnt/c/Users/xxxxxxPyro/gorpo"
VENV="$PROJECT_ROOT/.venv"
PID_FILE="$PROJECT_ROOT/gorpo.pid"
LOG_FILE="$PROJECT_ROOT/logs/gorpo.log"

cd "$PROJECT_ROOT"

case "${1:-start}" in
    start)
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "⚠️  Bot already running (PID: $(cat "$PID_FILE"))"
            exit 1
        fi
        
        echo "🚀 Starting Gorpo..."
        mkdir -p "$(dirname "$LOG_FILE")"
        
        # Run with setsid to detach from terminal
        setsid "$VENV/bin/python" -m bot.main > "$LOG_FILE" 2>&1 &
        PID=$!
        echo $PID > "$PID_FILE"
        
        echo "⏳ Waiting for Discord gateway connection (up to 45s)..."
        for i in $(seq 1 45); do
            if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
                echo "❌ Process died. Check logs:"
                tail -20 "$LOG_FILE"
                rm -f "$PID_FILE"
                exit 1
            fi
            if tail -5 "$LOG_FILE" 2>/dev/null | grep -q "Logged in as"; then
                echo "✅ Started (PID: $(cat "$PID_FILE"))"
                echo "📝 Logs: $LOG_FILE"
                exit 0
            fi
            sleep 1
        done
        echo "❌ Timeout waiting for login. Check logs:"
        tail -20 "$LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
        ;;
    stop)
        if [ ! -f "$PID_FILE" ]; then
            echo "⚠️  No pid file found. Bot may not be running."
            exit 0
        fi
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "🛑 Stopping Gorpo (PID: $PID)..."
            kill -TERM "$PID"
            for i in $(seq 1 10); do
                if ! kill -0 "$PID" 2>/dev/null; then break; fi
                sleep 1
            done
            if kill -0 "$PID" 2>/dev/null; then
                echo "⚠️  Force killing..."
                kill -KILL "$PID"
            fi
            echo "✅ Stopped"
        else
            echo "⚠️  Process not running (stale pid)"
        fi
        rm -f "$PID_FILE"
        ;;
    status)
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "✅ Gorpo is RUNNING (PID: $(cat "$PID_FILE"))"
            ps -p "$(cat "$PID_FILE")" -o pid,ppid,cmd
        else
            echo "❌ Gorpo is NOT running"
            exit 1
        fi
        ;;
    logs)
        mkdir -p "$(dirname "$LOG_FILE")"
        touch "$LOG_FILE"
        tail -f "$LOG_FILE"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac