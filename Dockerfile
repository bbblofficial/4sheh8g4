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

# --- Intercept Railway's uvicorn command ---
# تغییر نام باینری اصلی uvicorn و قرار دادن Wrapper هوشمند
RUN if [ -f /usr/local/bin/uvicorn ]; then \
        mv /usr/local/bin/uvicorn /usr/local/bin/uvicorn-real; \
    fi && \
    cat << 'EOF' > /usr/local/bin/uvicorn
#!/usr/bin/env bash
set -e

# ۱. راه‌اندازی و اتصال خودکار WARP
echo "[*] Initializing Cloudflare WARP..."
warp-svc &
sleep 2
warp-cli --accept-tos registration new || true
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port 40000
warp-cli --accept-tos connect

# ۲. تصحیح پارامترهای ورودی و جایگزینی کلمه متنی '$PORT' با مقدار عددی
REAL_PORT="${PORT:-8080}"
ARGS=()

for arg in "$@"; do
    if [ "$arg" = "\$PORT" ] || [ "$arg" = '$PORT' ]; then
        ARGS+=("$REAL_PORT")
    else
        ARGS+=("$arg")
    fi
done

echo "[*] Launching Uvicorn on port $REAL_PORT..."
exec /usr/local/bin/uvicorn-real "${ARGS[@]}"
EOF
RUN chmod +x /usr/local/bin/uvicorn
# ------------------------------------------

COPY entrypoint.sh .
COPY main.py .

RUN chmod +x entrypoint.sh

CMD ["bash", "./entrypoint.sh"]
