"""
Pushup detector — watches an RTSP camera stream 24/7 and automatically
counts pushups using MediaPipe Pose.  Sends completed sessions to the
API server.

Detection approach: tracks shoulder vertical position. When the body is
horizontal (pushup position), shoulder Y oscillates as the person goes
down and up. A hysteresis peak/valley detector counts each full cycle
as one rep.
"""

import os
import time
import logging
import threading
from pathlib import Path

import cv2
import mediapipe as mp
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import (
    PoseLandmarker,
    PoseLandmarkerOptions,
    PoseLandmark,
    RunningMode,
)

# ── Config ──────────────────────────────────────────────────────────────────
RTSP_URL = os.environ["RTSP_URL"]
API_URL = "http://localhost:8005/api/sessions"
LIVE_URL = "http://localhost:8005/api/live"
MODEL_PATH = str(Path(__file__).parent / "pose_landmarker_full.task")
PROCESS_EVERY_N = 3          # skip frames (lower = more accurate, higher = less CPU)
RESIZE_WIDTH = 640           # downscale HD frames before pose detection
SESSION_TIMEOUT = 10         # seconds of inactivity before session ends
RECONNECT_DELAY = 5          # seconds to wait before reconnecting
MIN_REPS = 2                 # discard sets with fewer reps (safety net)
POSITION_HOLD = 1.0          # must be horizontal for this long before counting reps

# Shoulder-tracking thresholds
HORIZONTAL_THRESHOLD = 0.10  # max shoulder–hip Y diff to count as pushup position
MOVE_THRESHOLD = 0.03        # shoulder Y must move this much to register a transition

DEBUG = True                 # log shoulder Y periodically

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("detector")


# ── Helpers ─────────────────────────────────────────────────────────────────
def send_live(event: dict):
    """Fire-and-forget POST of a live event to the server for WS broadcast."""
    def _post():
        try:
            httpx.post(LIVE_URL, json=event, timeout=2)
        except Exception:
            pass
    threading.Thread(target=_post, daemon=True).start()


def send_session(session: dict):
    """POST a completed session to the API server."""
    try:
        r = httpx.post(API_URL, json=session, timeout=5)
        r.raise_for_status()
        log.info("Session saved  (%d reps)", session["reps"])
    except Exception as e:
        log.error("Failed to save session: %s", e)


# ── Main loop ───────────────────────────────────────────────────────────────
def connect(url: str):
    """Open an RTSP capture, returning the VideoCapture or None."""
    log.info("Connecting to %s …", url.split("@")[-1])
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if cap.isOpened():
        log.info("Connected.")
        return cap
    log.warning("Connection failed.")
    cap.release()
    return None


def run():
    options = PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=MODEL_PATH),
        running_mode=RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.3,
        min_pose_presence_confidence=0.3,
        min_tracking_confidence=0.3,
    )
    landmarker = PoseLandmarker.create_from_options(options)

    cap = None
    frame_count = 0
    timestamp_ms = 0
    debug_counter = 0

    # Session state
    reps = 0
    session_start = None
    last_rep_time = None

    # Shoulder tracking state
    in_pushup_position = False
    in_down = False
    y_extreme = None  # tracks last extreme: min Y when UP, max Y when DOWN
    horizontal_since = None  # when body first became horizontal (for POSITION_HOLD)

    while True:
        # ── Reconnect if needed ─────────────────────────────────────────
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            cap = connect(RTSP_URL)
            if cap is None:
                time.sleep(RECONNECT_DELAY)
                continue

        ok, frame = cap.read()
        if not ok:
            log.warning("Frame read failed — reconnecting.")
            cap.release()
            cap = None
            time.sleep(RECONNECT_DELAY)
            continue

        frame_count += 1
        if frame_count % PROCESS_EVERY_N != 0:
            continue

        # ── Downscale HD frames for faster processing ───────────────────
        h, w = frame.shape[:2]
        if w > RESIZE_WIDTH:
            scale = RESIZE_WIDTH / w
            frame = cv2.resize(frame, (RESIZE_WIDTH, int(h * scale)))

        # ── Pose detection ──────────────────────────────────────────────
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

        # VIDEO mode requires monotonically increasing timestamps
        timestamp_ms += 33 * PROCESS_EVERY_N
        results = landmarker.detect_for_video(mp_image, timestamp_ms)

        if results.pose_landmarks and len(results.pose_landmarks) > 0:
            lm = results.pose_landmarks[0]

            # Average both shoulders and hips for stability
            sh_y = (lm[PoseLandmark.LEFT_SHOULDER].y + lm[PoseLandmark.RIGHT_SHOULDER].y) / 2
            hip_y = (lm[PoseLandmark.LEFT_HIP].y + lm[PoseLandmark.RIGHT_HIP].y) / 2
            diff = abs(sh_y - hip_y)

            # Only track shoulder movement when body is horizontal
            in_pushup_position = diff < HORIZONTAL_THRESHOLD

            if not in_pushup_position:
                # Standing/walking — reset shoulder tracking but NOT session
                in_down = False
                y_extreme = None
                horizontal_since = None
                continue

            # Track how long body has been continuously horizontal
            now = time.time()
            if horizontal_since is None:
                horizontal_since = now
            if now - horizontal_since < POSITION_HOLD:
                # Not horizontal long enough yet — ignore (filters standing-up transitions)
                continue

            # Initialize tracking on first valid frame
            if y_extreme is None:
                y_extreme = sh_y

            # Debug: log shoulder Y periodically
            if DEBUG:
                debug_counter += 1
                if debug_counter % 10 == 0:
                    log.info(
                        "[debug] sh_y=%.4f  state=%s  reps=%d",
                        sh_y, "DOWN" if in_down else "UP", reps,
                    )

            # ── Shoulder Y state machine (hysteresis peak/valley) ───────
            if not in_down:
                # UP state: track the highest point (lowest Y value)
                y_extreme = min(y_extreme, sh_y)
                if sh_y > y_extreme + MOVE_THRESHOLD:
                    in_down = True
                    y_extreme = sh_y
            else:
                # DOWN state: track the lowest point (highest Y value)
                y_extreme = max(y_extreme, sh_y)
                if sh_y < y_extreme - MOVE_THRESHOLD:
                    # Completed one rep (went down and came back up)
                    in_down = False
                    reps += 1
                    y_extreme = sh_y
                    now = time.time()

                    if session_start is None:
                        session_start = now
                        log.info("── Set started ──")
                        send_live({"type": "session_start"})

                    last_rep_time = now
                    log.info("↑ Rep %d  (sh_y=%.4f)", reps, sh_y)
                    send_live({"type": "rep", "reps": reps, "angle": 0})

        # ── Session timeout ─────────────────────────────────────────────
        if last_rep_time and (time.time() - last_rep_time > SESSION_TIMEOUT):
            duration = last_rep_time - session_start
            avg_pace = duration / reps if reps else 0

            if reps >= MIN_REPS:
                log.info(
                    "── Set ended ──  %d reps in %.0fs (%.1fs/rep)",
                    reps, duration, avg_pace,
                )

                send_live({
                    "type": "session_end",
                    "reps": reps,
                    "duration": round(duration, 1),
                })

                send_session({
                    "reps": reps,
                    "duration": round(duration, 1),
                    "start_time": session_start,
                    "end_time": last_rep_time,
                    "avg_pace": round(avg_pace, 2),
                })
            else:
                log.info("Discarded set with %d rep(s) (below min %d)", reps, MIN_REPS)
                send_live({"type": "session_end", "reps": 0})

            # Reset all state including horizontal tracking
            reps = 0
            in_down = False
            y_extreme = None
            in_pushup_position = False
            horizontal_since = None
            session_start = None
            last_rep_time = None


if __name__ == "__main__":
    log.info("Pushup detector starting …")
    try:
        run()
    except KeyboardInterrupt:
        log.info("Stopped.")
