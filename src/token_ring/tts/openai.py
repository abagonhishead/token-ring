"""Any server with OpenAI's speech API (POST /v1/audio/speech): OpenAI, Kokoro-FastAPI,
speaches, LocalAI, ... A voice is the server's voice name.

[tts.engines.kokoro]
type = "openai"
url = "http://127.0.0.1:8880/v1"   # base URL; default https://api.openai.com/v1
model = "kokoro"
voice = "bf_emma"                  # the default voice
api_key_env = "OPENAI_API_KEY"     # environment variable holding the key, if one is needed
format = "wav"                     # or "pcm" (raw 16-bit mono at sample_rate: streams sooner)
sample_rate = 24000                # for "pcm"
instructions = ""                  # optional, for models that take them (gpt-4o-mini-tts)
voices = []                        # the voices to offer on the web page; empty: asks the server
                                   # (GET /audio/voices, as Kokoro-FastAPI has), else OpenAI's own
"""
import json
import os
import urllib.request

from ..audio import FRAME_BYTES, from_wav, resample
from . import TTS, sentences


OPENAI_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"]


class OpenAITTS(TTS):
    def voices(self):
        names = self.conf.get("voices") or []
        if not names:
            try:
                req = urllib.request.Request(self.base() + "/audio/voices", headers=self.headers())
                with urllib.request.urlopen(req, timeout=10) as resp:
                    names = json.load(resp).get("voices", [])
            except Exception:
                names = OPENAI_VOICES if "api.openai.com" in self.base() else []
        names = [n if isinstance(n, str) else n.get("id") or n.get("name", "") for n in names]
        return [{"voice": n} for n in dict.fromkeys(filter(None, [*names, self.conf.get("voice", "")]))]

    def synthesize(self, text, voice, rate, emit, stop):
        for sentence in sentences(text):
            if stop.is_set():
                return
            self.one(sentence, voice, rate, emit, stop)

    def one(self, text, voice, rate, emit, stop):
        c = self.conf
        fmt = c.get("format", "wav")
        body = {"model": c.get("model", "tts-1"), "voice": voice or c.get("voice", "alloy"),
                "input": text, "response_format": fmt}
        if rate != 1:
            body["speed"] = rate
        if c.get("instructions"):
            body["instructions"] = c["instructions"]
        req = urllib.request.Request(self.base() + "/audio/speech", data=json.dumps(body).encode(),
                                     method="POST", headers={"Content-Type": "application/json", **self.headers()})
        with urllib.request.urlopen(req, timeout=c.get("timeout", 30)) as resp:
            if fmt == "pcm":
                sr, buf = c.get("sample_rate", 24000), b""
                while chunk := resp.read(FRAME_BYTES * 10):
                    if stop.is_set():
                        return
                    buf += chunk
                    whole = len(buf) // 2 * 2
                    emit(resample(buf[:whole], sr))
                    buf = buf[whole:]
            else:
                data = resp.read()
                if not stop.is_set():
                    emit(from_wav(data))

    def base(self):
        return self.conf.get("url", "https://api.openai.com/v1").rstrip("/")

    def headers(self):
        key = os.environ.get(self.conf.get("api_key_env", "OPENAI_API_KEY"), "")
        return {"Authorization": f"Bearer {key}"} if key else {}
