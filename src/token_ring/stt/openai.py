"""Any server with OpenAI's transcription API (POST /v1/audio/transcriptions): OpenAI, Groq,
speaches, faster-whisper-server, LocalAI, whisper.cpp's server (--inference-path), ...

[stt]
engine = "openai"
url = "http://127.0.0.1:8000/v1"   # base URL; default https://api.openai.com/v1
model = "Systran/faster-whisper-small"
api_key_env = "OPENAI_API_KEY"     # environment variable holding the key, if one is needed
language = "en"                    # optional
prompt = "Claude, tmux, SSH"       # optional: words to expect
"""
import json
import os
import urllib.request
import uuid

from ..audio import to_wav
from . import STT


class OpenAISTT(STT):
    def transcribe(self, pcm):
        c = self.conf
        fields = {"model": c.get("model", "whisper-1"), "response_format": "json"}
        for k in ("language", "prompt", "temperature"):
            if c.get(k) not in (None, ""):
                fields[k] = str(c[k])
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"speech.wav\"\r\n"
                 f"Content-Type: audio/wav\r\n\r\n").encode() + to_wav(pcm) + f"\r\n--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        key = os.environ.get(c.get("api_key_env", "OPENAI_API_KEY"), "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = c.get("url", "https://api.openai.com/v1").rstrip("/") + "/audio/transcriptions"
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=c.get("timeout", 30)) as resp:
            return json.load(resp).get("text", "")

    def describe(self):
        return f"{self.conf.get('url', 'OpenAI')} ({self.conf.get('model', 'whisper-1')})"
