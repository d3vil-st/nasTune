FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    curl \
    udev \
    util-linux \
    mount \
    && rm -rf /var/lib/apt/lists/*

# Install pre-built gpod-utils deb (apt resolves libgpod and glib deps automatically)
RUN curl -fsSL \
    https://github.com/d3vil-st/gpod-utils/releases/download/v1.4.4/gpod-utils_1.4.4.ubuntu26.04_amd64.deb \
    -o /tmp/gpod-utils.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/gpod-utils.deb \
    && rm /tmp/gpod-utils.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

RUN mkdir -p /mnt/ipod

ENV IPOD_MOUNT_POINT=/mnt/ipod

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
