# hailo-stt

Whisper speech-to-text on a Hailo-10H accelerator (the Raspberry Pi AI HAT+ 2), for the
token-ring bridge's `http` speech-to-text engine. Transcription runs on the accelerator, so it
costs almost no CPU: Whisper-Small takes about 1 to 2 s per utterance, Whisper-Base 0.4 to
0.8 s.

```toml
[stt]
engine = "http"
url = "http://stt.example.lan:9095/transcribe"
```

It needs HailoRT 5.x with the `hailo_platform.genai` Python bindings and a Whisper HEF built
for that HailoRT version. Settings are environment variables (see the top of `hailo_stt.py`):
`STT_MODEL`, `STT_PORT`, `STT_ALLOWED` (networks that may connect), `STT_TLS_DIR` (serve HTTPS
with `stt.crt` and `stt.key` from there).

The Hailo-10H serves one group of processes at a time. To share it with hailo-ollama, both
join the group `SHARED` (`STT_VDEVICE_GROUP_ID`, and `hailo-ollama-shared-vdevice.conf` as a
systemd drop-in for hailo-ollama).
