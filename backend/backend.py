# backend/db.py
import os
import json
import redis
from typing import Optional
from datetime import timedelta
import uuid

class RedisClient:
    def __init__(self, r):
        self.r = r

    @classmethod
    def from_url(cls, url):
        r = redis.from_url(url, decode_responses=True)
        return cls(r)

    def latest_key(self, device):
        return f"device:latest:{device}"

    def history_key(self, device):
        return f"device:history:{device}"

    def token_key(self, token):
        return f"token:{token}"

    def set_latest(self, device: str, payload: dict):
        k = self.latest_key(device)
        self.r.set(k, json.dumps(payload))
        # optional TTL: keep latest 48h by default (comment/uncomment)
        # self.r.expire(k, 48 * 3600)

    def get_latest(self, device: str) -> Optional[dict]:
        k = self.latest_key(device)
        v = self.r.get(k)
        return json.loads(v) if v else None

    def push_history(self, device: str, payload: dict, cap: int = 1000):
        k = self.history_key(device)
        self.r.lpush(k, json.dumps(payload))
        self.r.ltrim(k, 0, cap - 1)

    def create_token(self, device: str, ttl: int = 900) -> str:
        token = uuid.uuid4().hex[:12]
        k = self.token_key(token)
        self.r.set(k, device, ex=ttl)
        return token

    def consume_token(self, token: str) -> Optional[str]:
        """
        Atomically get and delete token (one-time).
        """
        k = self.token_key(token)
        # GET+DEL pattern in Lua to be atomic
        script = """
        local v = redis.call("GET", KEYS[1])
        if v then redis.call("DEL", KEYS[1]) end
        return v
        """
        res = self.r.eval(script, 1, k)
        return res
