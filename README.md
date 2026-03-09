# Pushup Counter

Automatic pushup counter using a security camera + MediaPipe Pose.
Watches your RTSP stream 24/7, counts pushups whenever you do them, and shows a real-time dashboard via WebSocket.

## Setup

```bash
cd pushup-counter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Download the MediaPipe pose model:

```bash
curl -L -o pose_landmarker_full.task \
  https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task
```

Copy `.env.example` to `.env` and set your RTSP camera URL:

```bash
cp .env.example .env
# Edit .env with your camera credentials
```

## Run

Both components at once:

```bash
./start.sh
```

Or separately:

```bash
# Terminal 1 — API + Dashboard
source .venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8005

# Terminal 2 — Detector
source .venv/bin/activate
python detector.py
```

Dashboard: **http://localhost:8005**

## How it works

- **detector.py** reads the RTSP stream, runs MediaPipe Pose, and tracks shoulder vertical position to count reps. When the body is horizontal (pushup position), shoulder Y oscillates — each down-up cycle counts as one rep. A set starts on the first rep and ends after 10s of inactivity, then gets saved to the API. Live events are broadcast via WebSocket so the dashboard updates in real time.
- **server.py** stores sets in SQLite, serves a dashboard with stats/chart/history, and provides a WebSocket endpoint for live updates.

## Tuning

In `detector.py`:
- `HORIZONTAL_THRESHOLD` — max shoulder-hip Y difference to count as pushup position (default 0.10)
- `MOVE_THRESHOLD` — shoulder Y movement needed to register a transition (default 0.03, increase if getting false reps)
- `SESSION_TIMEOUT` — seconds of inactivity before a set is saved (default 10)
- `PROCESS_EVERY_N` — skip frames to reduce CPU (default 3, increase if CPU is high)
- `POSITION_HOLD` — seconds body must be horizontal before counting starts (default 1.0, prevents false reps from standing up)
