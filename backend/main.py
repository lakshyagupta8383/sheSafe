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

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "supersecret")  # gateway signing secret or shared token
TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", "900"))  # 15 minutes default

app = FastAPI(title="Tracker Webhook API")

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
    lat: float
    lon: float
    timestamp: str
    status: str

@app.post("/api/webhook/sms")
async def webhook_sms(payload: WebhookPayload, request: Request,
                      x_provider_signature: Optional[str] = Header(None),
                      x_webhook_token: Optional[str] = Header(None)):
    """
    Webhook endpoint for SMS gateway. Accepts JSON with device, lat, lon.
    Gateway should post JSON. Validate either signature or header token.
    """
    body_bytes = await request.body()
    # verify signature or token (supports either)
    if not verify_signature_or_token(body=body_bytes,
                                     header_sig=x_provider_signature,
                                     header_token=x_webhook_token,
                                     secret=WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="invalid signature/token")

    # parse timestamp
    ts = payload.timestamp or datetime.now(timezone.utc).isoformat()
    # upsert latest state
    latest = {
        "lat": float(payload.lat),
        "lon": float(payload.lon),
        "timestamp": ts,
        "status": "active",
    }
    redis.set_latest(payload.device, latest)
    # append to history (capped list)
    redis.push_history(payload.device, latest)
    return {"ok": True}

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
