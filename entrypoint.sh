#!/usr/bin/env bash
set -e

echo "[*] Starting Cloudflare warp-svc daemon..."
warp-svc &
sleep 2

echo "[*] Initializing WARP client registration..."
warp-cli --accept-tos registration new || true

echo "[*] Switching WARP to local SOCKS5 proxy mode..."
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port 40000
warp-cli --accept-tos connect

echo "[*] Verifying SOCKS5 proxy connection to Cloudflare..."
for i in {1..20}; do
  TRACE=$(curl -s --socks5 127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace | grep "warp=" || true)
  if [[ "$TRACE" == "warp=on" || "$TRACE" == "warp=plus" ]]; then
    echo "[+] Cloudflare WARP SOCKS5 proxy verified and connected ($TRACE)!"
    break
  fi
  echo "[-] Waiting for WARP proxy tunnel... ($i/20)"
  sleep 1
done

# Strip any invalid non-numeric values passed into PORT
APP_PORT="${PORT:-8080}"
if ! [[ "$APP_PORT" =~ ^[0-9]+$ ]]; then
  echo "[!] Warning: Invalid PORT '$APP_PORT' detected, defaulting to 8080"
  APP_PORT=8080
fi

echo "[*] Launching Media Extraction Engine on port $APP_PORT..."
exec uvicorn main:app --host 0.0.0.0 --port "$APP_PORT"
