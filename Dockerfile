# Base Python + UV image
FROM ghcr.io/astral-sh/uv:python3.13-bookworm

# ---- Install Docker CLI and DinD dependencies ----
USER root
RUN apt-get update && apt-get install -y \
    docker.io \
    iptables \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ---- Add non-root user for running uvicorn ----
RUN adduser --disabled-password --gecos "" agent
WORKDIR /home/agent
COPY pyproject.toml uv.lock README.md ./
COPY src src
COPY docker docker

# ---- Install Python dependencies via uv ----
RUN --mount=type=cache,target=/home/agent/.cache/uv,uid=1000 \
    uv sync --locked

# ---- Copy startup script ----
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 9009

# ---- Use startup script to launch dockerd + uvicorn ----
ENTRYPOINT ["./start.sh"]
