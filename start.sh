#!/bin/sh

# Check if Docker socket is already available (mounted from host)
if docker info >/dev/null 2>&1; then
    echo "Using host Docker daemon."
else
    # Try to start Docker daemon (DinD) - may fail if not privileged
    echo "Starting Docker daemon..."
    dockerd --host=unix:///var/run/docker.sock &
    DOCKERD_PID=$!

    # Wait for dockerd to be ready (with timeout)
    echo "Waiting for Docker daemon..."
    TIMEOUT=10
    COUNTER=0
    while ! docker info >/dev/null 2>&1; do
        sleep 1
        COUNTER=$((COUNTER + 1))
        if [ $COUNTER -ge $TIMEOUT ]; then
            echo "Warning: Docker daemon not available after ${TIMEOUT}s. Continuing without Docker..."
            # Kill dockerd if it's still trying to start
            kill $DOCKERD_PID 2>/dev/null || true
            break
        fi
    done

    if docker info >/dev/null 2>&1; then
        echo "Docker daemon is ready."
    fi
fi

# Start UVicorn server (pass through all arguments)
uv run src/server.py "$@"
