#!/usr/bin/env python3
"""ECOWAN Firmware Compilation Bridge for Arduino CLI.

Provides a lightweight, zero-dependency local HTTP service that accepts
generated .ino sketch source from the ECOWAN Edge Monitor dashboard,
compiles it for ESP32 via arduino-cli, and streams the .bin firmware back
for browser download.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8027
DEFAULT_FQBN = "esp32:esp32:esp32"
COMPILE_LOCK = threading.Lock()

KNOWN_ARDUINO_CLI_PATHS = [
    Path(r"C:\Users\madly\AppData\Local\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"),
    Path.home() / "AppData/Local/Programs/Arduino IDE/resources/app/lib/backend/resources/arduino-cli.exe",
    Path.home() / "bin/arduino-cli",
    Path("/usr/local/bin/arduino-cli"),
    Path("/usr/bin/arduino-cli"),
]


def find_arduino_cli() -> str | None:
    system_which = shutil.which("arduino-cli")
    if system_which:
        return system_which
    for candidate in KNOWN_ARDUINO_CLI_PATHS:
        if candidate.is_file() and os.access(str(candidate), os.X_OK | os.R_OK):
            return str(candidate)
    return None


class CompileBridgeHandler(BaseHTTPRequestHandler):
    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        cli = find_arduino_cli()
        version_str = "unknown"
        if cli:
            try:
                res = subprocess.run([cli, "version"], capture_output=True, text=True, timeout=5)
                version_str = res.stdout.strip() or res.stderr.strip()
            except Exception as e:
                version_str = f"error: {e}"

        data = {
            "ok": bool(cli),
            "service": "ECOWAN Arduino CLI Compile Bridge",
            "arduinoCli": cli,
            "version": version_str,
            "fqbn": DEFAULT_FQBN,
        }
        resp = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(200 if cli else 503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(resp)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(resp)

    def do_POST(self):
        if self.path not in ("/api/compile", "/compile"):
            self.send_response(404)
            self.send_cors_headers()
            self.end_headers()
            return

        cli = find_arduino_cli()
        if not cli:
            err = json.dumps({
                "ok": False,
                "error": "arduino-cli executable not found on this machine. Ensure Arduino IDE or arduino-cli is installed."
            }).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(err)
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 5 * 1024 * 1024:
            self.send_response(400)
            self.send_cors_headers()
            self.end_headers()
            return

        try:
            raw_bytes = self.rfile.read(length)
            try:
                decoded_text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                decoded_text = raw_bytes.decode("utf-8", errors="replace")
            body = json.loads(decoded_text)
            sketch_name = str(body.get("sketchName", "NodeCore")).strip() or "NodeCore"
            sketch_source = str(body.get("sketchSource", "")).strip()
            fqbn = str(body.get("fqbn", DEFAULT_FQBN)).strip() or DEFAULT_FQBN
        except Exception as e:
            err = json.dumps({"ok": False, "error": f"Invalid JSON payload: {e}"}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(err)
            return

        if not sketch_source:
            err = json.dumps({"ok": False, "error": "Missing sketchSource in request"}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(err)
            return

        clean_name = "".join(c if c.isalnum() or c == "_" else "_" for c in sketch_name)
        if clean_name[0].isdigit():
            clean_name = "Node_" + clean_name

        # Single canonical persistent directories ensure Arduino CLI never invalidates
        # pre-compiled core and libraries (reducing build times from ~120s down to ~12-15s).
        canonical_base = Path(os.environ.get(
            "ECOWAN_BUILD_CACHE_BASE",
            "/var/cache/ecowan_canonical" if os.name != "nt" else str(Path(tempfile.gettempdir()) / "ecowan_canonical")
        ))
        canonical_sketch_dir = canonical_base / "sketch"
        canonical_sketch_dir.mkdir(parents=True, exist_ok=True)
        canonical_sketch_file = canonical_sketch_dir / "sketch.ino"

        safe_fqbn = fqbn.replace(":", "_")
        shared_build_dir = canonical_base / "build" / safe_fqbn
        shared_build_dir.mkdir(parents=True, exist_ok=True)

        output_dir = canonical_base / "out"
        output_dir.mkdir(parents=True, exist_ok=True)

        with COMPILE_LOCK:
            canonical_sketch_file.write_text(sketch_source, encoding="utf-8")

            cmd = [
                cli,
                "compile",
                "--fqbn", fqbn,
                "--build-path", str(shared_build_dir),
                "--output-dir", str(output_dir),
                "-j", "4",
                str(canonical_sketch_dir)
            ]

            print(f"[Compiler] Compiling {clean_name} (canonical slot) for {fqbn}...")
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300
                )
            except subprocess.TimeoutExpired:
                err = json.dumps({"ok": False, "error": "Compilation timed out after 300 seconds"}).encode("utf-8")
                self.send_response(504)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(err)))
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(err)
                return

            # Safety fallback: if cached compilation fails, wipe the build directory and retry clean once
            if proc.returncode != 0:
                print(f"[Compiler] Cached compile failed for {clean_name}, retrying with clean build cache...")
                shutil.rmtree(shared_build_dir, ignore_errors=True)
                shared_build_dir.mkdir(parents=True, exist_ok=True)
                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=300
                    )
                except subprocess.TimeoutExpired:
                    err = json.dumps({"ok": False, "error": "Clean compilation timed out after 300 seconds"}).encode("utf-8")
                    self.send_response(504)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(err)))
                    self.send_cors_headers()
                    self.end_headers()
                    self.wfile.write(err)
                    return

            if proc.returncode != 0:
                print(f"[Compiler] Error compiling {clean_name}:\n{proc.stderr}\n{proc.stdout}")
                err = json.dumps({
                    "ok": False,
                    "error": "Arduino compilation failed",
                    "details": proc.stderr or proc.stdout
                }).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(err)))
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(err)
                return

            bin_file = output_dir / "sketch.ino.bin"
            if not bin_file.is_file():
                bin_candidates = [
                    f for f in output_dir.glob("*.bin")
                    if not any(f.name.endswith(s) for s in ("bootloader.bin", "partitions.bin", "merged.bin"))
                ]
                if bin_candidates:
                    bin_file = bin_candidates[0]

            if not bin_file.is_file():
                err = json.dumps({
                    "ok": False,
                    "error": f"Binary output file not found in {output_dir}",
                    "details": proc.stdout
                }).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(err)))
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(err)
                return

            bin_bytes = bin_file.read_bytes()
            print(f"[Compiler] Successfully generated {clean_name}.ino.bin ({len(bin_bytes)} bytes)")

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{clean_name}.ino.bin"')
            self.send_header("Content-Length", str(len(bin_bytes)))
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(bin_bytes)


def main():
    parser = argparse.ArgumentParser(description="ECOWAN Local Compiler Bridge")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to listen on (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    cli = find_arduino_cli()
    if cli:
        print(f"[Bridge] Found arduino-cli: {cli}")
    else:
        print("[Bridge] WARNING: arduino-cli not found in PATH or standard paths!")

    server = ThreadingHTTPServer((args.host, args.port), CompileBridgeHandler)
    print(f"[Bridge] ECOWAN Compile Bridge listening on http://{args.host}:{args.port}")
    print("[Bridge] Ready to receive compilation requests from dashboard wizard.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Bridge] Shutting down...")


if __name__ == "__main__":
    main()
