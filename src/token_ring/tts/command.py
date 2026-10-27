"""Run a command per sentence: the text on its stdin, a WAV file on its stdout. "{voice}" in the
command is replaced by the voice.

[tts.engines.espeak]
type = "command"
command = ["espeak-ng", "-v", "{voice}", "--stdin", "--stdout"]
voice = "en-gb-x-rp"
"""
import subprocess

from ..audio import from_wav
from . import TTS, sentences


class CommandTTS(TTS):
    def synthesize(self, text, voice, rate, emit, stop):
        cmd = [a.replace("{voice}", voice or self.conf.get("voice", "")) for a in self.conf["command"]]
        for sentence in sentences(text):
            if stop.is_set():
                return
            out = subprocess.run(cmd, input=sentence.encode(), capture_output=True,
                                 timeout=self.conf.get("timeout", 30), check=True)
            if not stop.is_set():
                emit(from_wav(out.stdout))
