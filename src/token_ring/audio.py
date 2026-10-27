"""Audio helpers: 16 kHz, 16-bit mono little-endian PCM throughout."""
import io
import math
import wave

import numpy as np

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2  # 640 bytes


def tone(freq, ms, level=0.12, gap_ms=0):
    """A short sine beep (with soft edges), followed by gap_ms of silence."""
    t = np.arange(RATE * ms // 1000) / RATE
    env = np.minimum(1, np.minimum(t, t[::-1]) / 0.01)
    pcm = (np.sin(2 * np.pi * freq * t) * env * level * 32767).astype(np.int16).tobytes()
    return pcm + b"\0" * (RATE * gap_ms // 1000 * 2)


def frame_db(frame):
    x = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    rms = math.sqrt(float(np.mean(x * x))) if len(x) else 0.0
    return 20 * math.log10(max(rms, 1.0) / 32768)


def resample(pcm, rate, channels=1):
    """16-bit PCM at rate (interleaved if channels > 1) as 16 kHz mono."""
    x = np.frombuffer(pcm, dtype=np.int16)
    if channels > 1:
        x = x[: len(x) // channels * channels].reshape(-1, channels).mean(axis=1).astype(np.int16)
    if rate == RATE:
        return x.tobytes()
    import soxr
    return soxr.resample(x, rate, RATE).astype(np.int16).tobytes()


def to_wav(pcm, rate=RATE):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def from_wav(data):
    """A WAV file's audio as 16 kHz mono PCM."""
    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2:
            raise ValueError("only 16-bit WAV is supported")
        return resample(w.readframes(w.getnframes()), w.getframerate(), w.getnchannels())
