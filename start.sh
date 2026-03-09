#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Activate virtualenv
source .venv/bin/activate

echo "Starting pushup tracker…"

# Start API server in background
uvicorn server:app --host 0.0.0.0 --port 8005 &
SERVER_PID=$!
echo "  API server  → http://localhost:8005  (PID $SERVER_PID)"

# Give the server a moment to boot
sleep 2

# Start detector in foreground
echo "  Detector    → watching camera"
python detector.py &
DETECTOR_PID=$!

# Trap Ctrl-C to kill both
trap "echo 'Shutting down…'; kill $SERVER_PID $DETECTOR_PID 2>/dev/null; exit 0" INT TERM

wait
