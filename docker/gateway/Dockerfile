# The token-ring gateway with Claude Code, for a machine where the sessions also run in
# containers or where phone conversations only need the directories you mount. To reach Claude
# Code sessions running in tmux on the host, the gateway needs the host's view of them: see the
# gateway service in compose.yaml (or run token-ring-gateway on the host instead).
# Build from the repository root:  docker build -f docker/gateway/Dockerfile -t token-ring-gateway .
FROM python:3.13-slim-trixie
ARG UID=1000
RUN apt-get update && apt-get install -y --no-install-recommends tmux git curl ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*
# Claude Code's native build, installed for every user (auto-update is off: rebuild to update)
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && cp -L /root/.local/bin/claude /usr/local/bin/claude && rm -rf /root/.local/share/claude /root/.local/bin/claude
ENV DISABLE_AUTOUPDATER=1
COPY pyproject.toml README.md LICENSE /src/
COPY src /src/src
RUN pip install --no-cache-dir /src && rm -rf /src
COPY config.example.toml /etc/token-ring/config.toml
RUN groupadd --gid ${UID} token-ring && useradd --uid ${UID} --gid ${UID} --create-home token-ring
USER token-ring
CMD ["token-ring-gateway"]
