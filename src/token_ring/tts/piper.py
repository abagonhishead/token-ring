"""Piper (https://github.com/OHF-Voice/piper1-gpl) through its HTTP server
(`python -m piper.http_server`; docker/piper is an image of it). The server loads voices from its
data directory as they're asked for. A voice is "<model>" or "<model>:<speaker>" for
multi-speaker models.

[tts.engines.piper]
type = "piper"
url = "http://127.0.0.1:5000"
voice = "en_GB-alan-medium"   # the default voice
noise_scale = 0.333           # Piper's randomness (model defaults 0.667 / 0.8): lower keeps a
noise_w = 0.333               # reply sounding like one voice
"""
import json
import urllib.request

from ..audio import from_wav
from . import TTS, sentences


class PiperTTS(TTS):
    def synthesize(self, text, voice, rate, emit, stop):
        model, _, speaker = (voice or self.conf.get("voice", "")).partition(":")
        body = {"voice": model, "length_scale": 1 / max(rate, 0.1),
                "noise_scale": self.conf.get("noise_scale", 0.333),
                "noise_w_scale": self.conf.get("noise_w", 0.333)}
        if speaker:
            body["speaker"] = speaker
        url = self.conf.get("url", "http://127.0.0.1:5000").rstrip("/") + "/synthesize"
        for sentence in sentences(text):
            if stop.is_set():
                return
            req = urllib.request.Request(url, data=json.dumps(dict(body, text=sentence)).encode(),
                                         method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.conf.get("timeout", 30)) as resp:
                data = resp.read()
            if not stop.is_set():
                emit(from_wav(data))

    def voices(self):
        """Every model in the server's data directory, and each speaker of multi-speaker ones."""
        url = self.conf.get("url", "http://127.0.0.1:5000").rstrip("/") + "/voices"
        with urllib.request.urlopen(url, timeout=10) as resp:
            models = json.load(resp)
        out = []
        for model, info in sorted(models.items()):
            about = {"language": (info.get("language") or {}).get("code", ""),
                     "quality": (info.get("audio") or {}).get("quality", "")}
            speakers = info.get("speaker_id_map") or {}
            if info.get("num_speakers", 1) > 1 and speakers:
                out += [{"voice": f"{model}:{sp}", "speaker": sp, **about} for sp in sorted(speakers, key=speakers.get)]
            else:
                out.append({"voice": model, **about})
        return out

    def preload(self, voice):
        """Have the server load the voice (it loads them on first use)."""
        self.synthesize("Ready.", voice, 1.0, lambda pcm: None, _Never())


class _Never:
    @staticmethod
    def is_set():
        return False
