import os
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi import UploadFile, File, Form
from pydantic import BaseModel
from datetime import datetime, timezone
import json
from db import RedisClient
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import secrets
import re
from fastapi.responses import JSONResponse
import aiofiles
from fastapi import Query

# -------------------------
# Config
# -------------------------
DEVICE_UPLOAD_TOKEN = os.getenv("DEVICE_UPLOAD_TOKEN", "devtoken")
AUDIO_DIR = os.getenv("AUDIO_DIR", "/data/audio")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "supersecret")
TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", "900"))

# -------------------------
# App & storage setup
# -------------------------
app = FastAPI(title="Tracker Webhook API")
# Use Path (we imported it) instead of undefined 'pathlib'
Path(AUDIO_DIR).mkdir(parents=True, exist_ok=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# serve audio files (dev only)
app.mount("/static/audio", StaticFiles(directory=AUDIO_DIR), name="audio")

redis = RedisClient.from_url(REDIS_URL)

# -------------------------
# Models
# -------------------------
class MarkSafeRequest(BaseModel):
    device: str
    auth_token: Optional[str] = None

class LocationResponse(BaseModel):
    device: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    timestamp: Optional[str] = None
    status: str
    audio_url: Optional[str] = None
    audio_ts: Optional[str] = None

# -------------------------
# Helpers
# -------------------------
RE_TOKEN = re.compile(r"[?&]token=([A-Za-z0-9_\-]+)")

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# -------------------------
# Webhook: gateway → backend
# -------------------------
@app.post("/api/webhook/sms")
async def webhook_sms(request: Request, x_webhook_token: Optional[str] = Header(None)):
    """
    Gateway posts JSON:
    { "from": "+9199..", "raw_sms": "https://.../track?token=abc", "timestamp": "..." }
    Verifies gateway secret, extracts token, maps token→device, marks device active.
    """
    if x_webhook_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid webhook token")

    payload = await request.json()
    raw_sms = payload.get("raw_sms") or payload.get("text") or payload.get("body") or ""
    sender = payload.get("from")
    ts = payload.get("timestamp") or now_iso()

    m = RE_TOKEN.search(raw_sms)
    if not m:
        redis.r.lpush("unmapped:links", json.dumps({"raw": raw_sms, "from": sender, "ts": ts}))
        return {"ok": False, "reason": "no token in SMS"}

    token = m.group(1)

    try:
        device = redis.r.get(redis.token_key(token))
    except Exception:
        device = None

    if not device:
        redis.r.lpush("unmapped:links",
                      json.dumps({"raw": raw_sms, "from": sender, "ts": ts, "token": token}))
        return {"ok": False, "reason": "unknown token"}

    latest = redis.get_latest(device) or {}
    latest.update({
        "lat": latest.get("lat"),
        "lon": latest.get("lon"),
        "timestamp": ts,
        "status": "active",
        "last_sms": raw_sms,
        "sender": sender
    })
    redis.set_latest(device, latest)
    redis.push_history(device, {"event": "sos_via_link", "ts": ts, "sender": sender})

    return {"ok": True, "device": device}

# -------------------------
# Location API (frontend)
# -------------------------
@app.get("/api/location", response_model=LocationResponse)
async def get_location(device: str):
    rec = redis.get_latest(device)
    if not rec:
        raise HTTPException(status_code=404, detail="device not found")

    lat = float(rec["lat"]) if rec.get("lat") is not None else None
    lon = float(rec["lon"]) if rec.get("lon") is not None else None

    return LocationResponse(
        device=device,
        lat=lat,
        lon=lon,
        timestamp=rec.get("timestamp"),
        status=rec.get("status", "active"),
        audio_url=rec.get("audio_url"),
        audio_ts=rec.get("audio_ts")
    )

# -------------------------
# Token resolution (frontend)
# -------------------------
@app.get("/api/resolve-token")
async def resolve_token(token: str):
    device = redis.r.get(redis.token_key(token))
    if not device:
        return JSONResponse(status_code=404, content={"ok": False, "reason": "token not found"})

    latest = redis.get_latest(device) or {}
    return {"ok": True, "device": device, "latest": latest}

# -------------------------
# Mark device safe
# -------------------------
@app.post("/api/mark-safe")
async def mark_safe(req: MarkSafeRequest):
    rec = redis.get_latest(req.device)
    if not rec:
        raise HTTPException(status_code=404, detail="device not found")

    if req.auth_token:
        mapped = redis.consume_token(req.auth_token)
        if mapped != req.device:
            raise HTTPException(status_code=401, detail="invalid auth token")

    rec["status"] = "safe"
    rec["timestamp"] = now_iso()
    redis.set_latest(req.device, rec)
    redis.push_history(req.device, {"event": "marked_safe", "ts": rec["timestamp"]})

    return {"ok": True, "status": "safe"}

# -------------------------
# Token generation (dev / gateway)
# -------------------------
@app.post("/api/token/generate")
async def gen_token(device: str):
    token = redis.create_token(device, ttl=TOKEN_TTL_SECONDS)
    return {"token": token, "ttl_seconds": TOKEN_TTL_SECONDS}

# -------------------------
# Health
# -------------------------
@app.get("/health")
async def health():
    try:
        redis.r.ping()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

#--------------------------
#Location and audio file 
#writing on the db
#--------------------------

def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)

@app.post("/api/upload")
async def upload_with_location(
    device: str = Form(...),
    lat: Optional[float] = Form(None),
    lon: Optional[float] = Form(None),
    timestamp: Optional[str] = Form(None),
    file: UploadFile = File(None),
    x_device_token: Optional[str] = Header(None),
):
    """
    Accepts multipart/form-data fields:
      - device (str)
      - lat (float, optional)
      - lon (float, optional)
      - timestamp (ISO string, optional) -- gateway-supplied time
      - file (audio blob, optional)
    Must include header: X-Device-Token: <DEVICE_UPLOAD_TOKEN>
    """

    # auth
    if x_device_token != DEVICE_UPLOAD_TOKEN:
        raise HTTPException(status_code=401, detail="invalid device upload token")

    ts = timestamp or now_iso()

    # prepare latest record base
    latest = redis.get_latest(device) or {}
    latest["timestamp"] = ts
    latest["status"] = latest.get("status", "active")

    # update lat/lon if provided
    if lat is not None and lon is not None:
        latest["lat"] = float(lat)
        latest["lon"] = float(lon)
        redis.push_history(device, {"event": "location_update", "ts": ts, "lat": float(lat), "lon": float(lon)})

    audio_rel = None
    if file:
        # handle file (save to disk)
        filename_raw = file.filename or "audio"
        ext = Path(filename_raw).suffix or ".webm"
        ext = ext.lower()
        if ext not in (".webm", ".wav", ".mp3", ".ogg", ".m4a"):
            ext = ".webm"

        unique = secrets.token_urlsafe(8)
        out_name = f"{_safe_filename(device)}_{unique}{ext}"
        out_path = Path(AUDIO_DIR) / out_name

        try:
            async with aiofiles.open(out_path, "wb") as f:
                contents = await file.read()
                await f.write(contents)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"failed to save file: {e}")

        audio_rel = f"/static/audio/{out_name}"
        latest["audio_url"] = audio_rel
        latest["audio_ts"] = ts
        redis.push_history(device, {"event": "audio_upload", "ts": ts, "path": audio_rel})

    # persist combined latest
    redis.set_latest(device, latest)

    resp = {"ok": True, "device": device, "timestamp": ts}
    if audio_rel:
        resp["audio_url"] = audio_rel
        resp["audio_ts"] = ts
    return resp
