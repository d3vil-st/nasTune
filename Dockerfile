# ── Stage 1: minimal ffmpeg/ffprobe (see docker/build-ffmpeg.sh) ─────────────
FROM ubuntu:26.04 AS ffmpeg-builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends --no-install-suggests \
        build-essential \
        ca-certificates \
        git \
        nasm \
        pkg-config \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY docker/build-ffmpeg.sh /tmp/build-ffmpeg.sh
RUN /tmp/build-ffmpeg.sh /opt/ffmpeg

# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM ubuntu:26.04

ARG TARGETARCH=amd64
ARG GPOD_VERSION=1.6.1
ARG DISTRO=ubuntu26.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends --no-install-suggests \
        python3 \
        python3-venv \
        curl \
        jq \
        less \
        udev \
        util-linux \
        mount \
        hfsprogs \
        dosfstools \
        libgpod-common \
    && curl -fsSL \
      "https://github.com/d3vil-st/gpod-utils/releases/download/v${GPOD_VERSION}/gpod-utils_${GPOD_VERSION}.${DISTRO}_${TARGETARCH}.deb" \
      -o /tmp/gpod-utils.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends --no-install-suggests /tmp/gpod-utils.deb \
    && rm -rf /tmp/gpod-utils.deb /var/lib/apt/lists/*

COPY --from=ffmpeg-builder /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe /usr/local/bin/

WORKDIR /app

RUN mkdir -p /data

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY CHANGELOG.md .

ARG BUILD_VERSION=dev
ENV BUILD_VERSION=${BUILD_VERSION}

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
