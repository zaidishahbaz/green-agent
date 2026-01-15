# Dockerfile for bash execution container
# This container provides a persistent environment for:
# - Running bash commands (read/exec only)
# - Cloning and exploring repositories
# - Applying patches

ARG PYTHON_VERSION=3.9
# Use slim-buster for Python 3.6/3.7 (bookworm doesn't have these)
# Use slim-bookworm for Python 3.8+
FROM python:${PYTHON_VERSION}-slim-buster

# Fix for Debian buster EOL - use archive repositories
RUN echo "deb http://archive.debian.org/debian buster main" > /etc/apt/sources.list && \
    echo "deb http://archive.debian.org/debian-security buster/updates main" >> /etc/apt/sources.list && \
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid-until

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    libffi-dev \
    libssl-dev \
    curl \
    tree \
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
