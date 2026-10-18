#!/usr/bin/env bash
#
# PanelPlay -- one-line Proxmox installer.
# Run this ON THE PROXMOX HOST as root (not inside a container):
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/emtea01/panelplay/master/deploy/proxmox-install.sh)"
#
# It creates an unprivileged Debian 12 LXC, clones PanelPlay from GitHub and runs deploy/install.sh inside.
# It doesn't touch your comics or Plex folders: mount those into the container however suits you, then
# set their paths in the portal's Settings.
#
# Any setting below can be given as an environment variable, e.g.
#   CORES=2 CTID=120 bash -c "$(curl ...)"
#
# Written against documented pct/pveam behaviour but not yet run on a real Proxmox host:
# try it on a spare CTID first.
set -euo pipefail

GITHUB_REPO="${GITHUB_REPO:-https://github.com/emtea01/panelplay.git}"
BRANCH="${BRANCH:-master}"

CTID="${CTID:-}"                          # empty = next free ID
CT_HOSTNAME="${CT_HOSTNAME:-panelplay}"
INSTALL_KOKORO="${INSTALL_KOKORO:-0}"     # 1 = also install the local Kokoro voices (CPU PyTorch, ~2 GB)
CORES="${CORES:-4}"                       # ffmpeg encodes 1080p: more cores, faster renders
if [ "$INSTALL_KOKORO" = "1" ]; then
    MEMORY_MB="${MEMORY_MB:-4096}"; DISK_GB="${DISK_GB:-24}"
else
    MEMORY_MB="${MEMORY_MB:-2048}"; DISK_GB="${DISK_GB:-16}"
fi
SWAP_MB="${SWAP_MB:-1024}"
STORAGE="${STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
ROOT_PASSWORD="${ROOT_PASSWORD:-}"        # empty = generated, shown at the end
PORT="${PORT:-8077}"
APP_UID=1000                              # the service user inside the container; it is $((100000 + APP_UID)) on the host

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || { echo "!! Run this as root on the Proxmox host." >&2; exit 1; }
command -v pct >/dev/null 2>&1 || { echo "!! 'pct' not found: this doesn't look like a Proxmox host." >&2; exit 1; }

if [ -z "$ROOT_PASSWORD" ]; then
    ROOT_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20 || true)"
fi
[ -n "$CTID" ] || CTID="$(pvesh get /cluster/nextid)"
echo ">> Container $CTID: $CORES cores, ${MEMORY_MB} MB RAM, ${DISK_GB} GB disk"

# ---------------------------------------------------------------------------
# Debian 12 template (Python 3.11, which Kokoro also supports)
# ---------------------------------------------------------------------------
echo ">> Finding the Debian 12 template..."
pveam update >/dev/null 2>&1 || true
TEMPLATE="${TEMPLATE:-$(pveam available --section system 2>/dev/null | grep -o 'debian-12-standard[^ ]*' | sort -V | tail -1 || true)}"
if [ -z "$TEMPLATE" ]; then
    echo "!! Couldn't find a debian-12-standard template. Run 'pveam available' and set TEMPLATE=..." >&2; exit 1
fi
if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
    echo ">> Downloading $TEMPLATE ..."
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------
echo ">> Creating container $CTID ..."
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
    --hostname "$CT_HOSTNAME" \
    --ostype debian \
    --unprivileged 1 \
    --features nesting=1 \
    --cores "$CORES" \
    --memory "$MEMORY_MB" \
    --swap "$SWAP_MB" \
    --rootfs "${STORAGE}:${DISK_GB}" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
    --onboot 1 \
    --tags panelplay \
    --password "$ROOT_PASSWORD"

echo ">> Starting container ..."
pct start "$CTID"
echo ">> Waiting for the network..."
for _ in $(seq 1 30); do
    pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 && break
    sleep 2
done
pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 \
    || { echo "!! The container has no network (DHCP on $BRIDGE?). It's created; fix that and rerun deploy/install.sh inside." >&2; exit 1; }

# ---------------------------------------------------------------------------
# PanelPlay
# ---------------------------------------------------------------------------
echo ">> Cloning $GITHUB_REPO ($BRANCH) ..."
pct exec "$CTID" -- bash -c "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates curl"
pct exec "$CTID" -- git clone --depth 1 --branch "$BRANCH" "$GITHUB_REPO" /opt/panelplay

echo ">> Running deploy/install.sh inside the container ..."
pct exec "$CTID" -- env INSTALL_KOKORO="$INSTALL_KOKORO" APP_UID="$APP_UID" PORT="$PORT" bash /opt/panelplay/deploy/install.sh

CT_IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo ""
echo "======================================================================"
echo " PanelPlay is up:  http://${CT_IP}:${PORT}"
echo "   Container:      $CTID ($CT_HOSTNAME)"
echo "   Root password:  $ROOT_PASSWORD   (console: pct enter $CTID)"
echo ""
echo " Next:"
echo "   1. Mount your comics and Plex folders into the container, e.g. from a"
echo "      folder already mounted on this host:"
echo "        pct set $CTID -mp0 /path/on/host,mp=/path/in/container"
echo "      PanelPlay runs as uid $APP_UID in the container (uid $((100000 + APP_UID)) on"
echo "      this host): it needs to read the comics and write to the Plex folder."
echo "   2. In the portal's Settings, set both folders and add the Gemini and"
echo "      ElevenLabs keys (the Test buttons check each one)."
echo "   3. Story arcs: Import story index."
echo ""
echo " Update later with:  pct exec $CTID -- bash /opt/panelplay/deploy/redeploy.sh"
echo "======================================================================"
