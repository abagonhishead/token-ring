"""Run a command per utterance: a WAV file on its stdin, the text on its stdout.

[stt]
engine = "command"
command = ["whisper-cli", "-m", "/models/ggml-base.en.bin", "-f", "-", "-nt", "-np"]
"""
import subprocess

from ..audio import to_wav
from . import STT


class CommandSTT(STT):
    def transcribe(self, pcm):
        out = subprocess.run(self.conf["command"], input=to_wav(pcm), capture_output=True,
                             timeout=self.conf.get("timeout", 30), check=True)
        return out.stdout.decode(errors="replace").strip()

    def describe(self):
        return " ".join(self.conf["command"])
