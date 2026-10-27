# The token-ring bridge, beside Asterisk. Speech-to-text and text-to-speech are other services
# (see compose.yaml); trigger words use the small English Vosk model built in here ([keywords]).
# Build from the repository root:  docker build -f docker/bridge/Dockerfile -t token-ring-bridge .
# Your own image: FROM this, COPY your config.toml to /etc/token-ring/config.toml.
FROM python:3.13-slim-trixie
ARG UID=1000
ARG VOSK_MODEL=vosk-model-small-en-us-0.15
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates libatomic1 \
    && curl -fsSL -o /tmp/model.zip https://alphacephei.com/vosk/models/${VOSK_MODEL}.zip \
    && mkdir -p /opt/vosk && unzip -q /tmp/model.zip -d /opt/vosk && mv /opt/vosk/${VOSK_MODEL} /opt/vosk/model \
    && rm /tmp/model.zip && apt-get purge -y curl unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy soxr vosk
COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN pip install --no-cache-dir /src && rm -rf /src
COPY config.example.toml /etc/token-ring/config.toml
RUN groupadd --gid ${UID} token-ring && useradd --uid ${UID} --gid ${UID} --no-create-home token-ring
USER token-ring
# Shares /var/spool/asterisk with the Asterisk container (call files); host networking
CMD ["token-ring-bridge"]
