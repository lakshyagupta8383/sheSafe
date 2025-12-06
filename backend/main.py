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
import pathlib
import secrets
import re
from fastapi.responses import JSONResponse

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
pathlib.Path(AUDIO_DIR).mkdir(parents=True, exist_ok=True)

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