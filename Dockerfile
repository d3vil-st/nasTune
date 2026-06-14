FROM ubuntu:26.04

# Injected by Docker buildx — values: amd64, arm64
ARG TARGETARCH=amd64

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends --no-install-suggests \
    python3 \
    python3-venv \
    curl \
    udev \
    util-linux \
    mount \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install pre-built gpod-utils deb (apt resolves libgpod and glib deps automatically)
RUN curl -fsSL \
    https://github.com/d3vil-st/gpod-utils/releases/download/v1.4.4/gpod-utils_1.4.4.ubuntu26.04_${TARGETARCH}.deb \
    -o /tmp/gpod-utils.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends --no-install-suggests \
    /tmp/gpod-utils.deb \
    && rm /tmp/gpod-utils.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN mkdir -p /data

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
