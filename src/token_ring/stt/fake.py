"""For tests: returns canned transcripts in turn ([stt] replies = [...]), or the length of the
audio if there are none left."""
import threading

from . import STT


class FakeSTT(STT):
    def __init__(self, conf):
        super().__init__(conf)
        self.replies = list(conf.get("replies", []))
        self.lock = threading.Lock()

    def transcribe(self, pcm):
        with self.lock:
            return self.replies.pop(0) if self.replies else f"{len(pcm) / 32000:.1f} seconds of audio"
