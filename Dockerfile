FROM ubuntu:26.04

ARG TARGETARCH=amd64
ARG GPOD_VERSION=1.4.12
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
        ffmpeg \
        mount \
    && curl -fsSL \
      "https://github.com/d3vil-st/gpod-utils/releases/download/v${GPOD_VERSION}/gpod-utils_${GPOD_VERSION}.${DISTRO}_${TARGETARCH}.deb" \
      -o /tmp/gpod-utils.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends --no-install-suggests /tmp/gpod-utils.deb \
    && rm -rf /tmp/gpod-utils.deb /var/lib/apt/lists/*

WORKDIR /app

RUN mkdir -p /data

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

ARG BUILD_VERSION=dev
ENV BUILD_VERSION=${BUILD_VERSION}

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
