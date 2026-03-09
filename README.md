# Pushup Counter

Automatic pushup counter using a security camera (Tapo C210) + MediaPipe Pose.
Watches your RTSP stream 24/7 and counts pushups whenever you start doing them — no manual start/stop needed.

## Setup

```bash
cd pushup-counter
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Edit `detector.py` and set your RTSP credentials:

```python
RTSP_URL = "rtsp://YOUR_USER:YOUR_PASS@192.168.1.136:554/stream2"
```

## Run

Both components at once:

```bash
chmod +x start.sh
./start.sh
```

Or separately:

```bash
# Terminal 1 — API + Dashboard
uvicorn server:app --host 0.0.0.0 --port 8000

# Terminal 2 — Detector
python detector.py
```

Dashboard: **http://localhost:8000**

## How it works

- **detector.py** reads the RTSP stream, runs MediaPipe Pose on every 3rd frame, and tracks elbow angle to count reps (down < 90° → up > 155° = 1 rep). A session starts on the first rep and ends after 30s of inactivity, then gets POSTed to the API.
- **server.py** stores sessions in SQLite and serves a dashboard with stats, a 30-day chart, and session history.

## Tuning

In `detector.py`:
- `DOWN_ANGLE` / `UP_ANGLE` — adjust if reps aren't being detected (lower DOWN or raise UP for stricter detection)
- `SESSION_TIMEOUT` — seconds of inactivity before a session is saved (default 30)
- `PROCESS_EVERY_N` — skip frames to reduce CPU (default 3, increase if CPU is high)
