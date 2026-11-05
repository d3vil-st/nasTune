FROM ubuntu:26.04

ARG TARGETARCH=amd64
ARG GPOD_VERSION=1.6.3
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
      "https://github.com/d3vil-st/gpod-utils/releases/download/v${GPOD_VERSION}/gpod-utils-shared_${GPOD_VERSION}.${DISTRO}_${TARGETARCH}.deb" \
      -o /tmp/gpod-utils.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends --no-install-suggests /tmp/gpod-utils.deb \
    && rm -rf /tmp/gpod-utils.deb /var/lib/apt/lists/*

# gpod-utils-shared: all gpod-* tools share one private minimal ffmpeg 9.1, and it
# ships ffmpeg/ffprobe programs on those libs (playback transcode, artwork, tag
# reads) - used instead of the distro ffmpeg package (~355 MB of dependencies)
RUN ln -s /usr/lib/*/gpod-utils/ffmpeg /usr/lib/*/gpod-utils/ffprobe /usr/local/bin/ \
    && ffmpeg -hide_banner -version | head -1

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
