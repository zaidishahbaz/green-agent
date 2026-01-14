# Dockerfile for bash execution container
# This container provides a persistent environment for:
# - Running bash commands (read/exec only)
# - Cloning and exploring repositories
# - Applying patches

ARG PYTHON_VERSION=3.9
FROM python:${PYTHON_VERSION}-slim-bookworm

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    libffi-dev \
    libssl-dev \
    curl \
    tree \
    ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Install common Python dev tools
RUN pip install --no-cache-dir \
    pytest \
    pytest-xdist \
    pytest-timeout \
    pip-tools

# Create workspace directory
RUN mkdir -p /workspace

WORKDIR /workspace

# Default command keeps container running
CMD ["tail", "-f", "/dev/null"]
