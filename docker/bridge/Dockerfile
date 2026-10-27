# The token-ring bridge, beside Asterisk. Speech-to-text and text-to-speech are other services
# (see compose.yaml); trigger words use the small English Vosk model built in here ([keywords]).
# Build from the repository root:  docker build -f docker/bridge/Dockerfile -t token-ring-bridge .
# Your own image: FROM this, COPY your config.toml to /usr/share/token-ring/config.toml (brought
# into /etc/token-ring at each start, where the web page edits it; edits made there are kept).
FROM python:3.13-slim-trixie
ARG UID=1000
ARG VOSK_MODEL=vosk-model-small-en-us-0.15
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates libatomic1 \
    && curl -fsSL -o /tmp/model.zip https://alphacephei.com/vosk/models/${VOSK_MODEL}.zip \
    && mkdir -p /opt/vosk && unzip -q /tmp/model.zip -d /opt/vosk && mv /opt/vosk/${VOSK_MODEL} /opt/vosk/model \
    && rm /tmp/model.zip && apt-get purge -y curl unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
# Silero VAD ([vad] engine = "silero"), MIT licence
ARG SILERO_VERSION=v5.1.2
ARG SILERO_SHA256=2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f
ADD --checksum=sha256:${SILERO_SHA256} \
    https://github.com/snakers4/silero-vad/raw/${SILERO_VERSION}/src/silero_vad/data/silero_vad.onnx \
    /opt/silero/silero_vad.onnx
RUN chmod 644 /opt/silero/silero_vad.onnx && pip install --no-cache-dir numpy soxr vosk onnxruntime
COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN pip install --no-cache-dir /src && rm -rf /src
COPY config.example.toml /usr/share/token-ring/config.toml
RUN groupadd --gid ${UID} token-ring && useradd --uid ${UID} --gid ${UID} --no-create-home token-ring \
    && mkdir -p /etc/token-ring /var/log/token-ring && chown token-ring:token-ring /etc/token-ring /var/log/token-ring
USER token-ring
# Shares /var/spool/asterisk with the Asterisk container (call files); host networking
CMD ["token-ring-bridge"]
