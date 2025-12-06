import os
import time
import logging
import requests
import serial
import re
from datetime import datetime, timezone

# -----------------------
# Config (env)
# -----------------------
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
BAUDRATE = int(os.getenv("BAUDRATE", "115200"))
BACKEND_WEBHOOK = os.getenv("BACKEND_WEBHOOK", "http://localhost:8000/api/webhook/sms")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "3.0"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "8.0"))
DELETE_ON_SUCCESS = os.getenv("DELETE_ON_SUCCESS", "1") in ("1", "true", "True")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
# optional: allow filtering to only forward messages that match a prefix
SMS_FILTER_PREFIX = os.getenv("SMS_FILTER_PREFIX", "")  # e.g. "SOS"

# -----------------------
# Logging
# -----------------------
logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sim800-gateway")

# -----------------------
# Regex to parse AT+CMGL response header lines
# -----------------------
# Example header: +CMGL: 1,"REC UNREAD","+9112345","","21/12/01,12:34:56+32"
RE_CMGL = re.compile(r'^\+CMGL: *(\d+),"?([^",]*)"?,?\"?([^",]*)\"?,?.*')

def now_iso():
    return datetime.now(timezone.utc).isoformat()

# -----------------------
# Serial / Modem utilities
# -----------------------
class Sim800:
    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None

    def open(self):
        log.info("Opening serial port %s @ %d", self.port, self.baud)
        self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        time.sleep(0.3)
        self._drain()
        self._init_modem()

    def close(self):
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass

    def _drain(self):
        if not self.ser:
            return
        time.sleep(0.05)
        while self.ser.in_waiting:
            self.ser.read(self.ser.in_waiting)

    def _cmd(self, cmd: str, read_until_ok: bool = True, delay: float = 0.08):
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Serial port not open")
        self.ser.write((cmd + "\r\n").encode())
        time.sleep(delay)
        lines = []
        deadline = time.time() + 1.0
        while time.time() < deadline:
            line = self.ser.readline()
            if not line:
                break
            try:
                decoded = line.decode(errors="ignore").strip()
            except Exception:
                decoded = line.strip()
            if decoded != "":
                lines.append(decoded)
            if decoded == "OK" or decoded.startswith("ERROR"):
                break
        return lines

    def _init_modem(self):
        # basic init: echo off, SMS text mode, set charset
        try:
            self._cmd("AT")
            self._cmd("ATE0")
            self._cmd("AT+CMGF=1")  # text mode
            self._cmd('AT+CSCS="GSM"')
            # optional: set message indications to push new messages as unsolicited +CMTI
            # self._cmd('AT+CNMI=2,1,0,0,0')
            log.info("Modem initialized (text mode)")
        except Exception as e:
            log.warning("Failed to init modem: %s", e)

    def list_unread_sms(self):
        """
        Use AT+CMGL="REC UNREAD" to fetch unread messages.
        Returns list of tuples: (index:int, number:str, timestamp_iso:str, text:str)
        """
        lines = self._cmd('AT+CMGL="REC UNREAD"', delay=0.2)
        msgs = []
        i = 0
        while i < len(lines):
            line = lines[i]
            m = RE_CMGL.match(line)
            if m:
                try:
                    index = int(m.group(1))
                except Exception:
                    index = None
                number = m.group(3) or ""
                # next non-empty line is message text
                text = ""
                if i + 1 < len(lines):
                    text = lines[i + 1]
                    i += 1
                ts = now_iso()
                msgs.append((index, number, ts, text))
            i += 1
        return msgs

    def delete_sms(self, index: int):
        try:
            self._cmd(f'AT+CMGD={index}')
            log.debug("Deleted SMS index %d", index)
        except Exception as e:
            log.warning("Failed to delete SMS %d: %s", index, e)

# -----------------------
# HTTP forwarder
# -----------------------
def forward_sms(payload: dict):
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Token": WEBHOOK_SECRET
    }
    try:
        r = requests.post(BACKEND_WEBHOOK, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        log.info("Forwarded SMS from %s -> %s (status=%s)", payload.get("from"), BACKEND_WEBHOOK, r.status_code)
        if 200 <= r.status_code < 300:
            return True
        else:
            log.warning("Non-2xx response: %s %s", r.status_code, r.text[:200])
            return False
    except requests.RequestException as e:
        log.warning("HTTP error forwarding SMS: %s", e)
        return False

# -----------------------
# Main loop
# -----------------------
def main_loop():
    modem = Sim800(SERIAL_PORT, BAUDRATE, timeout=1.0)
    while True:
        try:
            if not modem.ser or not getattr(modem.ser, "is_open", False):
                try:
                    modem.open()
                except Exception as e:
                    log.error("Failed opening modem serial: %s — retrying in 5s", e)
                    time.sleep(5)
                    continue

            msgs = modem.list_unread_sms()
            if msgs:
                log.info("Found %d unread SMS", len(msgs))
            for idx, number, ts, text in msgs:
                try:
                    if SMS_FILTER_PREFIX and not text.startswith(SMS_FILTER_PREFIX):
                        log.info("Skipping SMS idx %s (prefix mismatch)", idx)
                        continue
                    payload = {"from": number, "raw_sms": text, "timestamp": ts}
                    ok = forward_sms(payload)
                    if ok and DELETE_ON_SUCCESS and idx is not None:
                        modem.delete_sms(idx)
                except Exception as e:
                    log.exception("Failed handling SMS index %s: %s", idx, e)
            time.sleep(POLL_INTERVAL)
        except serial.SerialException as e:
            log.error("Serial error: %s — reconnecting in 5s", e)
            try:
                modem.close()
            except Exception:
                pass
            time.sleep(5)
        except Exception as e:
            log.exception("Gateway error: %s — sleeping then retrying", e)
            time.sleep(5)

if __name__ == "__main__":
    log.info("Starting sim800 gateway (port=%s)", SERIAL_PORT)
    main_loop()