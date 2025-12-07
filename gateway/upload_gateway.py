#!/usr/bin/env python3
"""
gateway.py

Requirements:
  pip install requests python-dotenv pyserial   # pyserial optional
Run:
  export BACKEND_BASE=http://localhost:8000
  export DEVICE_ID=gateway-01
  export DEVICE_TOKEN=devtoken
  export WEBHOOK_SECRET=supersecret
  python3 gateway.py
"""

import os
import time
import json
import requests
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

# optional serial reading
try:
    import serial
except Exception:
    serial = None

# ---------- Configuration (from env) ----------
BACKEND_BASE = os.getenv("BACKEND_BASE", "http://localhost:8000")
BACKEND_WEBHOOK = os.getenv("BACKEND_WEBHOOK", BACKEND_BASE + "/api/webhook/sms")
BACKEND_TOKEN_GEN = os.getenv("BACKEND_TOKEN_GEN", BACKEND_BASE + "/api/token/generate")
BACKEND_UPLOAD = os.getenv("BACKEND_UPLOAD", BACKEND_BASE + "/api/upload")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
GEN_TOKEN_BEFORE_SMS = os.getenv("GEN_TOKEN_BEFORE_SMS", "true").lower() in ("1","true","yes")
DEVICE_ID = os.getenv("DEVICE_ID", "gateway-01")
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "devtoken")

# polling and paths
WATCH_DIR = Path(os.getenv("WATCH_DIR", "/tmp/gateway_out"))  # directory to watch for audio clips
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3"))      # seconds between directory polls
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "8"))
DELETE_ON_SUCCESS = os.getenv("DELETE_ON_SUCCESS", "1") == "1"
SMS_FILTER_PREFIX = os.getenv("SMS_FILTER_PREFIX", "#")     # optional prefix in serial messages indicating SMS
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
BAUDRATE = int(os.getenv("BAUDRATE", "115200"))

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ensure watch dir exists
WATCH_DIR.mkdir(parents=True, exist_ok=True)

# ---------- Helpers ----------
def now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def create_token(device: str) -> Optional[str]:
    """Call backend to generate a short-lived token for device."""
    try:
        resp = requests.post(BACKEND_TOKEN_GEN, params={"device": device}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token")
        logging.info("Created token %s for device %s", token, device)
        return token
    except Exception as e:
        logging.exception("Failed to create token: %s", e)
        return None

def post_webhook(raw_sms: str, sender: str = None, ts: Optional[str] = None) -> bool:
    """Post a JSON payload to the backend webhook endpoint."""
    payload = {"raw_sms": raw_sms}
    if sender:
        payload["from"] = sender
    payload["timestamp"] = ts or now_iso()
    headers = {}
    if WEBHOOK_SECRET:
        headers["X-Webhook-Token"] = WEBHOOK_SECRET
    try:
        logging.info("Posting webhook: %s", payload)
        r = requests.post(BACKEND_WEBHOOK, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        logging.info("webhook resp: %s %s", r.status_code, r.text)
        r.raise_for_status()
        return True
    except Exception:
        logging.exception("Failed to post webhook")
        return False

def upload_clip(audio_path: Path, lat: float, lon: float, timestamp: Optional[str] = None, max_retries:int=3):
    """Upload the audio + location to the backend upload endpoint."""
    ts = timestamp or now_iso()
    headers = {"X-Device-Token": DEVICE_TOKEN}
    data = {"device": DEVICE_ID, "lat": str(lat), "lon": str(lon), "timestamp": ts}
    files = {}
    if audio_path and audio_path.exists():
        files["file"] = (audio_path.name, open(audio_path, "rb"), "audio/webm")
    for attempt in range(1, max_retries+1):
        try:
            logging.info("Uploading %s (attempt %d)", audio_path, attempt)
            r = requests.post(BACKEND_UPLOAD, headers=headers, data=data, files=files if files else None, timeout=HTTP_TIMEOUT)
            logging.info("upload resp: %s %s", r.status_code, r.text)
            r.raise_for_status()
            # success
            if DELETE_ON_SUCCESS and audio_path and audio_path.exists():
                try:
                    audio_path.unlink()
                    logging.info("Deleted uploaded file %s", audio_path)
                except Exception:
                    logging.exception("Failed to delete file after upload")
            return r.json()
        except Exception:
            logging.exception("Upload attempt %d failed", attempt)
            time.sleep(attempt * 1.0)
    raise RuntimeError("upload failed after retries")

# ---------- Optional: serial reader (reads lines and triggers webhook) ----------
def run_serial_reader():
    if serial is None:
        logging.warning("pyserial not installed; serial reader disabled")
        return
    try:
        ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)
        logging.info("Opened serial %s @ %d", SERIAL_PORT, BAUDRATE)
    except Exception:
        logging.exception("Failed to open serial")
        return

    while True:
        try:
            line = ser.readline().decode(errors="ignore").strip()
            if not line:
                continue
            logging.debug("serial: %s", line)
            # filter based on prefix
            if SMS_FILTER_PREFIX and not line.startswith(SMS_FILTER_PREFIX):
                continue
            # assume the rest of line is an SMS-like string or URL
            raw_sms = line[len(SMS_FILTER_PREFIX):].strip() if line.startswith(SMS_FILTER_PREFIX) else line
            # optionally generate token and replace placeholder {token} in the SMS
            token = None
            if GEN_TOKEN_BEFORE_SMS:
                token = create_token(DEVICE_ID)
                if token:
                    raw_sms = raw_sms.replace("{token}", token)
            post_webhook(raw_sms)
        except Exception:
            logging.exception("serial loop exception")
            time.sleep(1)

# ---------- Directory watcher for audio clips ----------
def run_watch_folder():
    logging.info("Watching %s for audio clips (poll interval %.1fs)", WATCH_DIR, POLL_INTERVAL)
    seen = set()
    while True:
        try:
            files = sorted(WATCH_DIR.glob("*"), key=lambda p: p.stat().st_mtime)
            for p in files:
                if p.name in seen:
                    continue
                # basic filter: only regular files with plausible audio ext or any file
                if not p.is_file():
                    continue
                logging.info("Found candidate file %s", p)
                # parse coordinates from filename? or use a metadata JSON alongside file
                # For demo: expect a paired .meta JSON with same name + .meta (optional)
                meta_path = p.with_suffix(p.suffix + ".meta")
                lat, lon = 0.0, 0.0
                ts = None
                if meta_path.exists():
                    try:
                        m = json.loads(meta_path.read_text())
                        lat = float(m.get("lat", 0.0))
                        lon = float(m.get("lon", 0.0))
                        ts = m.get("timestamp")
                    except Exception:
                        logging.exception("Failed to parse meta file %s", meta_path)
                else:
                    # fallback: use env / static coords — replace with actual GPS reading logic
                    lat = float(os.getenv("STATIC_LAT", "28.7041"))
                    lon = float(os.getenv("STATIC_LON", "77.1025"))
                    ts = None
                try:
                    upload_clip(p, lat, lon, timestamp=ts)
                except Exception:
                    logging.exception("Failed to upload %s", p)
                finally:
                    seen.add(p.name)
            time.sleep(POLL_INTERVAL)
        except Exception:
            logging.exception("watch loop top-level")
            time.sleep(POLL_INTERVAL)

# ---------- Main ----------
def main():
    # Optionally spawn serial reader and/or folder watcher
    use_serial = os.getenv("USE_SERIAL", "0") in ("1","true","True")
    use_watch = os.getenv("USE_WATCH", "1") in ("1","true","True")

    if use_serial and serial:
        logging.info("Starting serial reader")
        run_serial_reader()
    elif use_watch:
        logging.info("Starting folder watcher")
        run_watch_folder()
    else:
        logging.error("Nothing to do (set USE_SERIAL or USE_WATCH)")

if __name__ == "__main__":
    main()
