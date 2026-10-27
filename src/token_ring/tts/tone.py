"""For tests and setting up: a short beep per word instead of speech (no model needed)."""
from ..audio import tone
from . import TTS


class ToneTTS(TTS):
    def synthesize(self, text, voice, rate, emit, stop):
        for _ in text.split():
            if stop.is_set():
                return
            emit(tone(440, int(60 / rate), level=0.05, gap_ms=int(40 / rate)))
