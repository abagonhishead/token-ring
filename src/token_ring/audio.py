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


def pitch_hz(pcm, rate=RATE):
    """The median pitch of the voiced parts of speech (16-bit mono PCM), by autocorrelation; 0 if
    there are none. Men's voices are mostly 85-155 Hz, women's 165-255 Hz."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    size, lo, hi = int(0.04 * rate), int(rate / 400), int(rate / 60)
    loud = 0.1 * (np.abs(x).max() if len(x) else 0)
    found = []
    for start in range(0, len(x) - size, size // 2):
        f = x[start:start + size]
        if np.abs(f).mean() < loud * 0.3:
            continue
        f = f - f.mean()
        ac = np.correlate(f, f, "full")[size - 1:]
        if ac[0] <= 0:
            continue
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] / ac[0] > 0.5:
            found.append(rate / lag)
    return float(np.median(found)) if found else 0.0
