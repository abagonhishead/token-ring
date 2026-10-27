# Piper text-to-speech (https://github.com/OHF-Voice/piper1-gpl, GPL-3.0) behind its own HTTP
# server, for the bridge's "piper" engine. Voices are downloaded at build time:
#   docker build -f docker/piper/Dockerfile --build-arg VOICES="en_GB-alan-medium en_US-lessac-medium" -t token-ring-piper .
# Your own image: FROM this, then RUN python -m piper.download_voices --data-dir /voices <more voices>
# or COPY your .onnx and .onnx.json files into /voices.
FROM python:3.13-slim-trixie
ARG VOICES="en_GB-alan-medium"
RUN pip install --no-cache-dir "piper-tts[http]"
RUN mkdir -p /voices && cd /voices && for v in ${VOICES}; do python -m piper.download_voices --data-dir /voices "$v"; done
ENV DEFAULT_VOICE=en_GB-alan-medium PORT=5000 HOST=127.0.0.1
RUN useradd --system --user-group --no-create-home piper
USER piper
# The default voice loads at start; others load on first use (each is ~60-120 MB of memory)
CMD python -m piper.http_server --host "$HOST" --port "$PORT" --data-dir /voices -m "$DEFAULT_VOICE"
