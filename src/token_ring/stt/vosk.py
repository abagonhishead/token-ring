"""Vosk, offline in this process: light enough for a Raspberry Pi's CPU, but much less accurate
than Whisper. Needs `pip install vosk` and a model from https://alphacephei.com/vosk/models.

[stt]
engine = "vosk"
model = "/models/vosk-model-small-en-us-0.15"
"""
import json

from ..audio import RATE
from . import STT


class VoskSTT(STT):
    def __init__(self, conf):
        super().__init__(conf)
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
        self.model = Model(conf["model"])

    def transcribe(self, pcm):
        from vosk import KaldiRecognizer
        rec = KaldiRecognizer(self.model, RATE)
        rec.AcceptWaveform(pcm)
        return json.loads(rec.FinalResult()).get("text", "")
