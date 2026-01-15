#!/bin/sh

# Check if Docker socket is already available (mounted from host)
if docker info >/dev/null 2>&1; then
    echo "Using host Docker daemon."
else
    # Start Docker daemon (DinD)
    echo "Starting Docker daemon..."
    dockerd --host=unix:///var/run/docker.sock &

    # Wait for dockerd to be ready
    echo "Waiting for Docker daemon..."
    while ! docker info >/dev/null 2>&1; do
        sleep 1
    done
    echo "Docker daemon is ready."
fi

# Start UVicorn server (pass through all arguments)
uv run src/server.py "$@"
