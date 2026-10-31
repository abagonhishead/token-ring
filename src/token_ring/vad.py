"""Speech detection: which 20 ms frames of the caller's audio are their speech.

[vad]
engine = "energy"                  # loudness above the background (built in)
                                   # "silero": Silero VAD, a small neural model (in the bridge
                                   # image), which ignores noise that isn't speech
model = "/opt/silero/silero_vad.onnx"
threshold = 0.5                    # silero: the speech probability that counts as speech
near_db = 12                       # once the caller's voice level is known (from what they've
                                   # said on this call), only count speech at most this much
                                   # quieter: a TV or people further away don't (0: off)
min_level_db = -100                # and never anything quieter than this (dBFS). The bridge logs
                                   # each utterance's level: set it between your voice and the TV
noise_rise_db = 0                  # how fast the background level may rise (dB a second), so the
                                   # start of speech the detector isn't sure of yet doesn't raise
                                   # it (0: as fast as it falls)
record_dir = ""                    # save each utterance as a WAV here, with the 2 s before it, to
record_keep = 200                  # hear what the phone sent; the newest record_keep are kept

Another engine is a VAD subclass named as the engine ("module:Class") or registered as a
token_ring.vad entry point.
"""
import logging

from . import config, plugins
from .audio import RATE

log = logging.getLogger("token-ring")


class VAD:
    """One per call. speech(frame, above_db, starting) is called for every 20 ms frame the caller
    is listened to: whether it's speech, given how far above the background noise it is (dB), and
    whether it would start an utterance (stricter) or continue one."""

    START_DB = 15

    def __init__(self, conf):
        self.conf = conf

    def speech(self, frame, above_db, starting):
        return above_db > (self.START_DB if starting else self.START_DB - 5)


class EnergyVAD(VAD):
    pass


class SileroVAD(VAD):
    """Silero VAD (https://github.com/snakers4/silero-vad, MIT): a speech probability for each
    32 ms of audio, with onnxruntime. Speech must also be a little above the background."""

    CHUNK = 512    # samples at 16 kHz
    CONTEXT = 64   # samples of the previous chunk the model wants in front of each one
    _session = None

    def __init__(self, conf):
        super().__init__(conf)
        import numpy as np
        self.np = np
        self.session = self.load(conf.get("model") or "/opt/silero/silero_vad.onnx")
        self.state = np.zeros((2, 1, 128), np.float32)
        self.context = np.zeros((1, self.CONTEXT), np.float32)
        self.pending = np.zeros(0, np.float32)
        self.sr = np.array(RATE, np.int64)
        self.prob = 0.0

    @classmethod
    def load(cls, path):
        if cls._session is None or cls._session[0] != path:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = opts.inter_op_num_threads = 1
            cls._session = (path, ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"]))
        return cls._session[1]

    def update(self, frame):
        np = self.np
        self.pending = np.concatenate([self.pending, np.frombuffer(frame, "<i2").astype(np.float32) / 32768])
        while len(self.pending) >= self.CHUNK:
            x, self.pending = self.pending[None, :self.CHUNK], self.pending[self.CHUNK:]
            out, self.state = self.session.run(
                None, {"input": np.concatenate([self.context, x], 1), "state": self.state, "sr": self.sr})
            self.context = x[:, -self.CONTEXT:]
            self.prob = float(out[0][0])

    def speech(self, frame, above_db, starting):
        self.update(frame)
        threshold = self.conf.get("threshold", 0.5)
        # a speech-like sound only a few dB above the background is more likely noise
        return self.prob >= (threshold if starting else threshold - 0.15) and above_db > (6 if starting else 3)


def create():
    conf = config.section("vad")
    name = conf.get("engine", "energy")
    try:
        return plugins.load("vad", name)(conf)
    except Exception as e:
        log.warning("speech detection %r unavailable (%s); using loudness", name, e)
        return EnergyVAD(conf)
