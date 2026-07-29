#!/bin/sh
# mojatools-agent installer. Downloads the pinned agent release, verifies its
# SHA256, installs as an unprivileged system user, enrolls, and sets up a
# systemd timer. Requires root. POSIX sh, no bashisms.
set -eu
umask 022

AGENT_USER="mojatools-agent"
INSTALL_DIR="/opt/mojatools-agent"
CONFIG_DIR="/etc/mojatools-agent"
STATE_DIR="/var/lib/mojatools-agent"
AGENT_URL="https://raw.githubusercontent.com/mojatools/mojatools-agent/v1.0.0/mojatools_agent.py"
SERVER=""
ENROLL_TOKEN=""
EXPECTED_SHA256=""
ENABLE_DOCKER=0

usage() {
    echo "usage: install.sh --server URL --expected-sha256 HEX [--enroll-token T] [--enable-docker]" >&2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --server) SERVER="$2"; shift 2 ;;
        --enroll-token) ENROLL_TOKEN="$2"; shift 2 ;;
        --expected-sha256) EXPECTED_SHA256="$2"; shift 2 ;;
        --agent-url) AGENT_URL="$2"; shift 2 ;;
        --enable-docker) ENABLE_DOCKER=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" = "0" ] || { echo "run as root (sudo)" >&2; exit 1; }

ALREADY_ENROLLED=0
if [ -f "$CONFIG_DIR/agent.json" ] && [ -f "$CONFIG_DIR/token" ]; then
    ALREADY_ENROLLED=1
fi

if [ -z "$SERVER" ] || [ -z "$EXPECTED_SHA256" ]; then
    usage
    exit 2
fi
if [ "$ALREADY_ENROLLED" = "0" ] && [ -z "$ENROLL_TOKEN" ]; then
    echo "error: --enroll-token is required (not currently enrolled)" >&2
    usage
    exit 2
fi

[ -x /usr/bin/python3 ] || {
    echo "python3 at /usr/bin/python3 is required (install python3 via your distro package manager)" >&2
    exit 1
}
command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required" >&2; exit 1; }

if [ -x /usr/sbin/nologin ]; then
    NOLOGIN=/usr/sbin/nologin
else
    NOLOGIN=/sbin/nologin
fi

echo "==> downloading agent from $AGENT_URL"
TMP_AGENT="$(mktemp)"
trap 'rm -f "$TMP_AGENT"' EXIT
curl -fsSL "$AGENT_URL" -o "$TMP_AGENT"

ACTUAL_SHA256="$(sha256sum "$TMP_AGENT" | cut -d' ' -f1)"
if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
    echo "FATAL: checksum mismatch (expected $EXPECTED_SHA256, got $ACTUAL_SHA256)" >&2
    exit 1
fi
echo "==> checksum OK"

if ! id "$AGENT_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell "$NOLOGIN" "$AGENT_USER"
else
    usermod -s "$NOLOGIN" "$AGENT_USER"
fi
if [ "$ENABLE_DOCKER" = "1" ]; then
    echo "WARNING: adding $AGENT_USER to the docker group. The docker group is"
    echo "effectively root-equivalent on this host. Enable only if you need"
    echo "container metrics."
    usermod -aG docker "$AGENT_USER"
fi

install -d -m 0755 "$INSTALL_DIR"
install -d -m 0770 -o root -g "$AGENT_USER" "$CONFIG_DIR"
install -d -m 0750 -o "$AGENT_USER" -g "$AGENT_USER" "$STATE_DIR"
install -m 0755 -o root -g root "$TMP_AGENT" "$INSTALL_DIR/mojatools_agent.py"

if [ "$ALREADY_ENROLLED" = "1" ]; then
    echo "==> already enrolled, skipping enrollment (upgrade mode)"
else
    HOSTNAME="$(hostname)"
    echo "==> enrolling $HOSTNAME on $SERVER"
    su -s /bin/sh "$AGENT_USER" -c \
        'exec python3 /opt/mojatools-agent/mojatools_agent.py enroll --server "$1" --enroll-token "$2" --hostname "$3"' \
        sh "$SERVER" "$ENROLL_TOKEN" "$HOSTNAME"
    chmod 0640 "$CONFIG_DIR/agent.json"
    chown root:"$AGENT_USER" "$CONFIG_DIR/agent.json"
fi

echo "==> installing systemd units"
# (the two unit files are embedded below and written verbatim)
cat > /etc/systemd/system/mojatools-agent.service <<'EOF'
[Unit]
Description=mojatools inside-out monitoring agent
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=mojatools-agent
ExecStart=/usr/bin/python3 /opt/mojatools-agent/mojatools_agent.py run
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/mojatools-agent
EOF

cat > /etc/systemd/system/mojatools-agent.timer <<'EOF'
[Unit]
Description=run mojatools agent every minute (self-throttled by server config)

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now mojatools-agent.timer

echo "==> done. Check with: systemctl status mojatools-agent.timer ; journalctl -u mojatools-agent.service -n 20"
