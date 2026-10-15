#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo ./install.sh"
  exit 1
fi
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP=/root/edge_dash.py
HTML=/root/index.html
WRAPPER=/usr/bin/dashboard
CONFIG=/etc/default/edge-monitor
UNIT=/etc/systemd/system/edge-monitor.service
STAMP=$(date +%Y%m%d-%H%M%S)

[ -f "$SRC/edge_dash.py" ] && [ -f "$SRC/index.html" ] || { echo "Missing edge_dash.py or index.html beside install.sh"; exit 1; }

# Backup the files currently used by the user's existing dashboard, if present.
mkdir -p "/root/edge-monitor-backups/$STAMP"
for f in "$APP" "$HTML" "$WRAPPER" "$CONFIG" "$UNIT"; do
  if [ -f "$f" ]; then cp -p "$f" "/root/edge-monitor-backups/$STAMP/$(basename "$f")"; fi
done

# Stop only the existing dashboard service/process. Do not kill unrelated Python apps.
if command -v systemctl >/dev/null 2>&1; then systemctl stop edge-monitor.service 2>/dev/null || true; fi
if command -v pkill >/dev/null 2>&1; then pkill -f '[e]dge_dash.py' 2>/dev/null || true; fi

install -m 0755 "$SRC/edge_dash.py" "$APP"
install -m 0644 "$SRC/index.html" "$HTML"
install -m 0755 "$SRC/dashboard" "$WRAPPER"

if [ ! -f "$CONFIG" ]; then
  cat > "$CONFIG" <<'EOF'
# Edge Monitor settings. Edit this file, then run: dashboard restart
DASH_HOST=0.0.0.0
DASH_PORT=8026
DASH_HTML_FILE=/root/index.html
DASH_HISTORY_FILE=/root/edge_monitor_state.json
DASH_LATE_SECONDS=90
DASH_OFFLINE_SECONDS=180
DASH_SAVE_SECS=5
DASH_SERIES_DAYS=7
DASH_MQTT_ENABLED=1
DASH_MQTT_HOST=127.0.0.1
DASH_MQTT_PORT=1883
DASH_MQTT_USER=
DASH_MQTT_PASSWORD=
DASH_MQTT_TOPIC='application/+/device/+/event/+'
EOF
  chmod 0640 "$CONFIG"
fi

if ! command -v mosquitto_sub >/dev/null 2>&1; then
  echo "mosquitto_sub not found; trying to install the mosquitto-clients package."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get install -y mosquitto-clients || echo "WARNING: package install failed. HTTP webhook mode still works; install mosquitto-clients to enable MQTT."
  else
    echo "WARNING: install the Mosquitto client tools for MQTT. HTTP webhook mode still works."
  fi
fi

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  cat > "$UNIT" <<'EOF'
[Unit]
Description=Edge Monitor - ChirpStack LoRaWAN dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
EnvironmentFile=-/etc/default/edge-monitor
ExecStart=/usr/bin/python3 -u /root/edge_dash.py
Restart=always
RestartSec=3
KillSignal=SIGINT
TimeoutStopSec=15
WorkingDirectory=/root

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable edge-monitor.service
  systemctl restart edge-monitor.service
  sleep 2
  if systemctl is-active --quiet edge-monitor.service; then
    echo "Edge Monitor installed and running as a systemd service."
  else
    echo "Service did not stay up. Recent service log:"
    journalctl -u edge-monitor.service -n 50 --no-pager || true
    echo "Inspect with: dashboard logs"
    exit 1
  fi
else
  echo "systemd unavailable; starting with the dashboard fallback process manager."
  "$WRAPPER" bg
fi

cat <<EOF

Installed files:
  Backend:  $APP
  UI:       $HTML
  Config:   $CONFIG
  Backups:  /root/edge-monitor-backups/$STAMP

Useful commands:
  dashboard status
  dashboard logs
  dashboard restart
  dashboard down

Open the dashboard at http://<server-LAN-IP>:8026/
The server has no login screen; keep port 8026 on a trusted LAN/VPN and do not port-forward it to the public internet.
EOF
