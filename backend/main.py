# backend/main.py
import os
import hmac
import hashlib
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from datetime import datetime, timezone
import json
from db import RedisClient
from verify import verify_signature_or_token
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi import UploadFile, File, Form
import pathlib
import secrets

DEVICE_UPLOAD_TOKEN = os.getenv("DEVICE_UPLOAD_TOKEN", "devtoken")  # header token devices use to upload audio
AUDIO_DIR = os.getenv("AUDIO_DIR", "/data/audio")                  # where audio files are stored (dev)
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "supersecret")  # gateway signing secret or shared token
TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", "900"))  # 15 minutes default

app = FastAPI(title="Tracker Webhook API")
pathlib.Path(AUDIO_DIR).mkdir(parents=True, exist_ok=True)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.mount("/static/audio", StaticFiles(directory=AUDIO_DIR), name="audio")
redis = RedisClient.from_url(REDIS_URL)

class WebhookPayload(BaseModel):
    device: str
    lat: float
    lon: float
    timestamp: Optional[str] = None
    raw_sms: Optional[str] = None

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

RE_TOKEN = re.compile(r"[?&]token=([A-Za-z0-9_\-]+)")

@app.post("/api/webhook/sms")
async def webhook_sms(request: Request, x_webhook_token: Optional[str] = Header(None)):
    """
    Gateway posts JSON:
    { "from": "+9199..", "raw_sms": "https://.../track?token=abc", "timestamp": "..." }
    We verify X-Webhook-Token, extract token, map to device, and mark device active.
    """
    if x_webhook_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid webhook token")

    payload = await request.json()
    raw_sms = payload.get("raw_sms") or payload.get("text") or ""
    sender = payload.get("from")
    ts = payload.get("timestamp") or now_iso()

    m = RE_TOKEN.search(raw_sms)
    if not m:
        r.lpush("unmapped:links", json.dumps({"raw": raw_sms, "from": sender, "ts": ts}))
        return {"ok": False, "reason": "no token in SMS"}

    token = m.group(1)
    device = r.get(token_key(token))
    if not device:
        r.lpush("unmapped:links", json.dumps({"raw": raw_sms, "from": sender, "ts": ts, "token": token}))
        return {"ok": False, "reason": "unknown token"}

    # mark device active; preserve any existing lat/lon
    latest = get_latest(device) or {}
    latest.update({
        "lat": latest.get("lat"),
        "lon": latest.get("lon"),
        "timestamp": ts,
        "status": "active",
        "last_sms": raw_sms,
        "sender": sender
    })
    set_latest(device, latest)
    push_history(device, {"event": "sos_via_link", "ts": ts, "sender": sender})
    return {"ok": True, "device": device}

@app.get("/api/location", response_model=LocationResponse)
async def get_location(device: str):
    rec = redis.get_latest(device)
    if not rec:
        raise HTTPException(status_code=404, detail="device not found")
    return LocationResponse(device=device,
                            lat=float(rec["lat"]),
                            lon=float(rec["lon"]),
                            timestamp=rec["timestamp"],
                            status=rec.get("status", "active"))
@app.get("/api/resolve-token")
async def resolve_token(token: str):
    """
    Map token -> device. Frontend calls this when landing on /track?token=...
    """
    device = r.get(token_key(token))
    if not device:
        return JSONResponse(status_code=404, content={"ok": False, "reason": "token not found"})
    latest = get_latest(device) or {}
    return {"ok": True, "device": device, "latest": latest}

@app.post("/api/mark-safe")
async def mark_safe(req: MarkSafeRequest):
    rec = redis.get_latest(req.device)
    if not rec:
        raise HTTPException(status_code=404, detail="device not found")
    # if token present, validate it maps to device
    if req.auth_token:
        mapped = redis.consume_token(req.auth_token)
        if mapped != req.device:
            raise HTTPException(status_code=401, detail="invalid auth token")
    # mark safe and update timestamp
    rec["status"] = "safe"
    rec["timestamp"] = datetime.now(timezone.utc).isoformat()
    redis.set_latest(req.device, rec)
    redis.push_history(req.device, rec)
    return {"ok": True, "status": "safe"}

# Dev helper: generate short link token
@app.post("/api/token/generate")
async def gen_token(device: str):
    token = redis.create_token(device, ttl=TOKEN_TTL_SECONDS)
    return {"token": token, "ttl_seconds": TOKEN_TTL_SECONDS}
