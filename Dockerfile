FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install system utilities, gpg, curl, and ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gpg \
    lsb-release \
    ca-certificates \
    ffmpeg \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install official Cloudflare WARP client
RUN curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ bookworm main" | tee /etc/apt/sources.list.d/cloudflare-client.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends cloudflare-warp && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY entrypoint.sh .
COPY main.py .

RUN chmod +x entrypoint.sh

# Run startup script inside bash shell
CMD ["/bin/bash", "./entrypoint.sh"]
