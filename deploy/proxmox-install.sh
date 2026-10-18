#!/usr/bin/env bash
#
# PanelPlay -- one-line Proxmox installer.
# Run this ON THE PROXMOX HOST as root (not inside a container):
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/emtea01/panelplay/master/deploy/proxmox-install.sh)"
#
# It creates an unprivileged Debian 12 LXC, bind-mounts your comics (read-only) and your Plex
# TV folder (read-write) into it, clones PanelPlay from GitHub and runs deploy/install.sh inside.
# Mount the NAS shares on the host first (see README, "Proxmox"); this script doesn't mount them.
#
# Any setting below can be given as an environment variable, e.g.
#   COMICS_PATH=/mnt/pve/comics PLEX_PATH=/mnt/pve/anime bash -c "$(curl ...)"
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

# Paths ON THIS HOST where the NAS shares are already mounted.
COMICS_PATH="${COMICS_PATH:-}"            # the comics library, or "none" if PanelPlay will read from Komga
PLEX_PATH="${PLEX_PATH:-}"                # the Plex TV library folder episodes are written to
APP_UID=1000                              # the service user inside the container; it is $((100000 + APP_UID)) on the host

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || { echo "!! Run this as root on the Proxmox host." >&2; exit 1; }
command -v pct >/dev/null 2>&1 || { echo "!! 'pct' not found: this doesn't look like a Proxmox host." >&2; exit 1; }

if [ -z "$COMICS_PATH" ]; then
    read -rp "Path on this host where your comics are mounted (e.g. /mnt/pve/comics), or 'none' to use Komga: " COMICS_PATH
fi
if [ "$COMICS_PATH" != "none" ] && [ ! -d "$COMICS_PATH" ]; then
    echo "!! $COMICS_PATH doesn't exist or isn't mounted. Mount the NAS share there first." >&2; exit 1
fi
if [ -z "$PLEX_PATH" ]; then
    read -rp "Path on this host where your Plex TV folder is mounted (e.g. /mnt/pve/anime): " PLEX_PATH
fi
[ -d "$PLEX_PATH" ] || { echo "!! $PLEX_PATH doesn't exist or isn't mounted. Mount the NAS share there first." >&2; exit 1; }

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

COMICS_IN_CT=""
if [ "$COMICS_PATH" != "none" ]; then
    echo ">> Comics: $COMICS_PATH -> /mnt/comics (read-only)"
    pct set "$CTID" -mp0 "${COMICS_PATH},mp=/mnt/comics,ro=1"
    COMICS_IN_CT="/mnt/comics"
    # Point the portal at the 2000 AD folder itself, so other titles' issue numbers don't pass for progs.
    for d in "2000 AD (1977)" "2000 AD" "2000AD"; do
        if [ -d "$COMICS_PATH/$d" ]; then COMICS_IN_CT="/mnt/comics/$d"; break; fi
    done
fi
echo ">> Plex: $PLEX_PATH -> /mnt/plex (read-write)"
pct set "$CTID" -mp1 "${PLEX_PATH},mp=/mnt/plex"

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
pct exec "$CTID" -- env INSTALL_KOKORO="$INSTALL_KOKORO" APP_UID="$APP_UID" PORT="$PORT" \
    COMICS_IN_CT="$COMICS_IN_CT" OUTPUT_IN_CT=/mnt/plex bash /opt/panelplay/deploy/install.sh

CT_IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo ""
echo "======================================================================"
echo " PanelPlay is up:  http://${CT_IP}:${PORT}"
echo "   Container:      $CTID ($CT_HOSTNAME)"
echo "   Root password:  $ROOT_PASSWORD   (console: pct enter $CTID)"
echo ""
echo " Next, in the portal:"
echo "   Settings: add the Gemini and ElevenLabs keys (Test buttons check them)."
echo "   Story arcs: Import story index."
echo ""
echo " If the Plex folder test says it isn't writable, mount that share on"
echo " this host with uid=$((100000 + APP_UID)),gid=$((100000 + APP_UID)) (see README)."
echo ""
echo " Update later with:  pct exec $CTID -- bash /opt/panelplay/deploy/redeploy.sh"
echo "======================================================================"
