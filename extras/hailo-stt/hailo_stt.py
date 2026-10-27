#!/usr/bin/env python3
"""Speech-to-text on the Hailo-10H: Whisper via hailo_platform.genai.Speech2Text over HTTPS.

POST /transcribe with a body of raw 16 kHz, 16-bit, mono little-endian PCM.
Returns JSON: {"text": "...", "ms": <transcription time>, "audio_s": <length>}.
GET /health returns the model in use.

HTTPS if STT_TLS_DIR holds stt.crt and stt.key, otherwise plain HTTP.
The model stays loaded on the accelerator; requests are handled one at a time.
"""
import ipaddress
import json
import logging
import os
import re
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from hailo_platform import VDevice
from hailo_platform.genai import Speech2Text, Speech2TextTask

HOST = os.environ.get("STT_HOST", "0.0.0.0")
PORT = int(os.environ.get("STT_PORT", "9095"))
MODEL = os.environ.get("STT_MODEL", "/opt/hailo-whisper/Whisper-Small.hef")
# Who may connect (space-separated networks)
ALLOWED = [ipaddress.ip_network(n) for n in os.environ.get("STT_ALLOWED", "127.0.0.0/8").split()]
TLS_DIR = os.environ.get("STT_TLS_DIR", "")
# The Hailo-10H serves one VDevice group at a time; processes in the same group share it.
# hailo-ollama joins via HAILO_OLLAMA_VDEVICE_GROUP_ID=SHARED (systemd drop-in).
GROUP_ID = os.environ.get("STT_VDEVICE_GROUP_ID", "SHARED")
MIN_SECONDS = 0.1
MAX_SECONDS = 30

log = logging.getLogger("hailo-stt")


def drop_repeats(text):
    """Whisper sometimes loops and repeats a sentence; keep the first of any consecutive repeats."""
    parts = re.findall(r"[^.?!]+[.?!]*", text)
    kept, last = [], None
    for p in parts:
        key = re.sub(r"\W+", " ", p).strip().lower()
        if key and key != last:
            kept.append(p.strip())
        last = key or last
    return " ".join(kept)


class Engine:
    def __init__(self, model):
        self.lock = threading.Lock()
        params = VDevice.create_params()
        params.group_id = GROUP_ID
        self.vdevice = VDevice(params)
        self.s2t = Speech2Text(self.vdevice, model)
        self.model = os.path.basename(model)

    def transcribe(self, pcm):
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        with self.lock:
            t0 = time.perf_counter()
            raw = self.s2t.generate_all_text(audio, task=Speech2TextTask.TRANSCRIBE, language="en")
            ms = (time.perf_counter() - t0) * 1000
        text = drop_repeats(raw.strip())
        if text != raw.strip():
            log.info("dropped repeat: %r -> %r", raw.strip(), text)
        return text, ms, len(audio) / 16000


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"model": engine.model})
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        if not any(ipaddress.ip_address(self.client_address[0]) in n for n in ALLOWED):
            return self.reply(403, {"error": "forbidden"})
        if self.path != "/transcribe":
            return self.reply(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        if length % 2 or not MIN_SECONDS * 16000 * 2 <= length <= MAX_SECONDS * 16000 * 2:
            return self.reply(400, {"error": f"body must be {MIN_SECONDS}-{MAX_SECONDS} s of 16 kHz s16le PCM"})
        text, ms, secs = engine.transcribe(self.rfile.read(length))
        log.info("%s: %.1f s audio -> %d ms: %r", self.client_address[0], secs, ms, text)
        self.reply(200, {"text": text, "ms": round(ms), "audio_s": round(secs, 2)})

    def log_message(self, *args):
        pass  # requests are logged in do_POST


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    engine = Engine(MODEL)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    if TLS_DIR:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(os.path.join(TLS_DIR, "stt.crt"), os.path.join(TLS_DIR, "stt.key"))
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    log.info("listening on %s://%s:%d, model %s, allowed %s", "https" if TLS_DIR else "http", HOST, PORT, engine.model,
             " ".join(map(str, ALLOWED)))
    server.serve_forever()
