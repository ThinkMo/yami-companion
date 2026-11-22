#!/usr/bin/env bash
# One-command ESP-Mosaico S31 firmware build & flash helper for Yami Learning Companion.
#
# Usage:
#   ./scripts/flash_mosaico.sh                          # Auto-detect LAN IP & USB port, build + flash
#   ./scripts/flash_mosaico.sh --backend http://192.168.1.20:8000 --port /dev/cu.usbmodem1101
#   ./scripts/flash_mosaico.sh --build-only             # Compile only (no board connected)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FW_DIR="$ROOT_DIR/firmware/esp-mosaico-vibe/projects/yami"
BACKEND_URL=""
SERIAL_PORT=""
BUILD_ONLY=0
MONITOR=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend) BACKEND_URL="$2"; shift 2 ;;
    --port) SERIAL_PORT="$2"; shift 2 ;;
    --build-only) BUILD_ONLY=1; shift ;;
    --monitor) MONITOR=1; shift ;;
    -h|--help)
      echo "Usage: $0 [--backend http://<LAN_IP>:8000] [--port /dev/cu.usbmodem*] [--build-only] [--monitor]"
      exit 0
      ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ ! -d "$FW_DIR" ]]; then
  echo "ERROR: Firmware project not found at: $FW_DIR"
  echo "Please ensure firmware/esp-mosaico-vibe/projects/yami is present."
  exit 1
fi

# 1. Source ESP-IDF if idf.py not already in PATH
if ! command -v idf.py >/dev/null 2>&1; then
  for idf_candidate in \
    "${IDF_PATH:-}/export.sh" \
    "$HOME/esp/esp-idf/export.sh" \
    "$HOME/esp/v5.4/esp-idf/export.sh" \
    "$HOME/esp/v5.3/esp-idf/export.sh"; do
    if [[ -f "$idf_candidate" ]]; then
      echo "==> Loading ESP-IDF from $idf_candidate"
      # shellcheck disable=SC1090
      source "$idf_candidate" >/dev/null
      break
    fi
  done
fi

if ! command -v idf.py >/dev/null 2>&1; then
  echo "ERROR: idf.py not found. Please install ESP-IDF v5.3+ and run 'source \$IDF_PATH/export.sh' first."
  exit 1
fi

# 2. Auto-detect host LAN IP for Mosaico -> Backend connection
if [[ -z "$BACKEND_URL" ]]; then
  LAN_IP=""
  if command -v ipconfig >/dev/null 2>&1; then
    LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  elif command -v hostname >/dev/null 2>&1; then
    LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  if [[ -z "$LAN_IP" ]]; then
    echo "ERROR: Could not auto-detect LAN IP. Pass --backend http://<YOUR_LAN_IP>:8000"
    exit 1
  fi
  BACKEND_URL="http://${LAN_IP}:8000"
fi

echo "==> Target YAMI_BACKEND_URL: $BACKEND_URL"

# 3. Auto-detect USB serial port if flashing
if [[ "$BUILD_ONLY" -eq 0 && -z "$SERIAL_PORT" ]]; then
  SERIAL_PORT="$(ls /dev/cu.usbmodem* /dev/cu.usbserial* /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -n 1 || true)"
  if [[ -z "$SERIAL_PORT" ]]; then
    echo "ERROR: No ESP-Mosaico USB serial port detected."
    echo "Connect ESP-Mosaico via USB-C data cable, or pass --port <DEV> / --build-only."
    exit 1
  fi
  echo "==> Detected serial port: $SERIAL_PORT"
fi

cd "$FW_DIR"
export EXTRA_CFLAGS="-DYAMI_BACKEND_URL=\\\"${BACKEND_URL}\\\""

echo "==> Building ESP-Mosaico Yami firmware..."
idf.py build

if [[ "$BUILD_ONLY" -eq 1 ]]; then
  echo "✅ Build finished (--build-only)."
  exit 0
fi

echo "==> Flashing to $SERIAL_PORT..."
if [[ "$MONITOR" -eq 1 ]]; then
  idf.py -p "$SERIAL_PORT" flash monitor
else
  idf.py -p "$SERIAL_PORT" flash
  echo ""
  echo "✅ Flash complete!"
  echo "Next: Power on Mosaico, read the 6-digit pairing code on screen, and bind it in Guardian Web -> 设置 -> Mosaico 设备绑定."
fi
