"""The simplest possible STT server protocol: POST the raw audio (16 kHz, 16-bit mono
little-endian PCM, Content-Type application/octet-stream), get back JSON {"text": "..."}.
Wrap any engine (an accelerator, a model of your own) in a small server that speaks it.

[stt]
engine = "http"
url = "https://stt.example.lan:9095/transcribe"
ca_file = ""                       # CA bundle for a private CA
token_env = ""                     # environment variable with a bearer token, if needed
"""
import json
import os
import ssl
import urllib.request

from . import STT


class HTTPSTT(STT):
    def transcribe(self, pcm):
        c = self.conf
        headers = {"Content-Type": "application/octet-stream"}
        if c.get("token_env") and os.environ.get(c["token_env"]):
            headers["Authorization"] = f"Bearer {os.environ[c['token_env']]}"
        ctx = ssl.create_default_context(cafile=c.get("ca_file") or None) if c["url"].startswith("https:") else None
        req = urllib.request.Request(c["url"], data=pcm, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=c.get("timeout", 15), context=ctx) as resp:
            return json.load(resp)["text"]

    def describe(self):
        return self.conf["url"]
