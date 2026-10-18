#!/usr/bin/env bash
# PanelPlay installer. Run INSIDE the Debian LXC as root, from the cloned repo at /opt/panelplay
# (proxmox-install.sh does this for you). Safe to run again.
#
#   INSTALL_KOKORO=1   also install the local Kokoro voices (CPU PyTorch, ~2 GB); ElevenLabs needs neither
#   COMICS_IN_CT / OUTPUT_IN_CT   comics and Plex folders as seen inside the container, used on first start
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR=/var/lib/panelplay
APP_UID="${APP_UID:-1000}"
PORT="${PORT:-8077}"
INSTALL_KOKORO="${INSTALL_KOKORO:-0}"

echo ">> System packages..."
apt-get update -qq
PKGS="python3 python3-venv python3-pip ffmpeg fonts-dejavu-core git curl ca-certificates"
[ "$INSTALL_KOKORO" = "1" ] && PKGS="$PKGS espeak-ng"
# shellcheck disable=SC2086
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $PKGS

echo ">> Service user 'panelplay' (uid $APP_UID)..."
if ! id panelplay >/dev/null 2>&1; then
    useradd --uid "$APP_UID" --user-group --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin panelplay
fi
mkdir -p "$DATA_DIR"
chown panelplay:panelplay "$DATA_DIR"

echo ">> Python environment..."
[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
if [ "$INSTALL_KOKORO" = "1" ]; then
    echo ">> Kokoro (CPU PyTorch first, to skip the CUDA download)..."
    "$APP_DIR/venv/bin/pip" install -q torch --index-url https://download.pytorch.org/whl/cpu
    "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements-kokoro.txt"
fi

# Folders the portal starts with (first start only; change them later in Settings).
if [ ! -f /etc/panelplay.env ]; then
    {
        echo "# Read by panelplay.service. The paths only seed the portal's settings on its first start."
        [ -n "${COMICS_IN_CT:-}" ] && echo "PANELPLAY_COMICS_PATH=${COMICS_IN_CT}"
        [ -n "${OUTPUT_IN_CT:-}" ] && echo "PANELPLAY_OUTPUT_PATH=${OUTPUT_IN_CT}"
        echo "PANELPLAY_PORT=${PORT}"
    } > /etc/panelplay.env
fi

echo ">> systemd service..."
cp "$APP_DIR/deploy/panelplay.service" /etc/systemd/system/panelplay.service
systemctl daemon-reload
systemctl enable -q panelplay
systemctl restart panelplay

echo ">> Waiting for the portal..."
for _ in $(seq 1 30); do
    curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/arcs" && break
    sleep 1
done
if ! curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/arcs"; then
    echo "!! The portal didn't answer. Check: journalctl -u panelplay -n 50" >&2
    exit 1
fi

if [ -n "${OUTPUT_IN_CT:-}" ] && [ -d "$OUTPUT_IN_CT" ]; then
    if runuser -u panelplay -- sh -c "touch '$OUTPUT_IN_CT/.panelplay-test' && rm '$OUTPUT_IN_CT/.panelplay-test'" 2>/dev/null; then
        echo ">> $OUTPUT_IN_CT is writable."
    else
        echo "!! The service user can't write to $OUTPUT_IN_CT. On the Proxmox host, mount that share"
        echo "!! with uid=$((100000 + APP_UID)),gid=$((100000 + APP_UID)) (see README, Proxmox)."
    fi
fi
echo ">> Done: http://$(hostname -I | awk '{print $1}'):${PORT}"
