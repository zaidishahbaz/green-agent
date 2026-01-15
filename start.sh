#!/bin/sh
# Start Docker daemon (DinD)
dockerd --host=unix:///var/run/docker.sock &

# Wait for dockerd to be ready
echo "Waiting for Docker daemon..."
while ! docker info >/dev/null 2>&1; do
    sleep 1
done
echo "Docker daemon is ready."

# Start UVicorn server
uv run src/server.py --host 0.0.0.0 --port 9009
