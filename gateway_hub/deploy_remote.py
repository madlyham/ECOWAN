"""Deploy ECOWAN 3.0 to Dragino LoRaWAN Gateway and LaCave Build/Vision Server."""

import subprocess
import time
import urllib.request
import json

DRAGINO_IP = "100.83.203.103"
LACAVE_IP = "100.93.135.116"

FILES = [
    "gateway_hub/edge_dash.py",
    "gateway_hub/index.html",
    "gateway_hub/payload_codec.py",
    "gateway_hub/vision_engine.py",
    "gateway_hub/vision_transport.py",
    "gateway_hub/vision_hub.py",
    "gateway_hub/realsense_node.py",
]

def run_ssh(host, cmd):
    res = subprocess.run(
        ["ssh.exe", "-o", "StrictHostKeyChecking=no", f"root@{host}", cmd],
        capture_output=True,
        text=True
    )
    return res.stdout.strip(), res.stderr.strip(), res.returncode

def scp_files(host, files, remote_dir, legacy_scp=False):
    scp_cmd = ["scp.exe", "-o", "StrictHostKeyChecking=no"]
    if legacy_scp:
        scp_cmd.append("-O")
    scp_cmd.extend(files)
    scp_cmd.append(f"root@{host}:{remote_dir}")
    res = subprocess.run(scp_cmd, capture_output=True, text=True)
    return res.returncode == 0, res.stderr.strip()

def deploy_dragino():
    print(f"\n--- Deploying to Dragino ({DRAGINO_IP}) ---")
    # 1. Update DASH_AUTH_PASSWORD to empty so no login gate blocks users
    out, err, code = run_ssh(DRAGINO_IP, "sed -i 's/DASH_AUTH_PASSWORD=.*/DASH_AUTH_PASSWORD=\"\"/' /etc/default/edge-monitor")
    print(f"Auth password disabled: code={code}")

    # 2. Upload files
    ok, err = scp_files(DRAGINO_IP, FILES, "/root/", legacy_scp=True)
    if not ok:
        print(f"SCP to Dragino failed: {err}")
        return False
    print("Files uploaded successfully.")

    # 3. Restart edge-monitor service
    out, err, code = run_ssh(DRAGINO_IP, "/etc/init.d/edge-monitor restart 2>/dev/null || (killall python3; /usr/bin/python3 -u /root/edge_dash.py &)")
    print(f"Service restarted: {out}")
    time.sleep(3)

    # 4. Probe healthz
    try:
        req = urllib.request.urlopen(f"http://{DRAGINO_IP}:8026/healthz", timeout=6)
        data = json.loads(req.read().decode())
        print(f"Dragino Health: {data}")
        return data.get("version") == "3.0.0"
    except Exception as e:
        print(f"Health check failed on Dragino: {e}")
        return False

def deploy_lacave():
    print(f"\n--- Deploying to LaCave ({LACAVE_IP}) ---")
    run_ssh(LACAVE_IP, "mkdir -p /root/ecowan_vision")
    ok, err = scp_files(LACAVE_IP, FILES, "/root/ecowan_vision/", legacy_scp=False)
    if not ok:
        print(f"SCP to LaCave failed: {err}")
        return False
    print("Files uploaded successfully.")

    out, err, code = run_ssh(LACAVE_IP, "systemctl restart ecowan-vision.service")
    print(f"Systemd restart: code={code}")
    time.sleep(2)

    try:
        req = urllib.request.urlopen(f"http://{LACAVE_IP}:8026/healthz", timeout=6)
        data = json.loads(req.read().decode())
        print(f"LaCave Health: {data}")
        return data.get("version") == "3.0.0"
    except Exception as e:
        print(f"Health check failed on LaCave: {e}")
        return False

if __name__ == "__main__":
    d_ok = deploy_dragino()
    l_ok = deploy_lacave()
    print(f"\nSummary: Dragino={d_ok}, LaCave={l_ok}")
