#!/usr/bin/env python3
"""Edge Monitor: ChirpStack v4 MQTT + HTTP uplink dashboard backend.

Standard-library-only backend for the accompanying index.html. MQTT ingest uses
mosquitto_sub so the service can run on small gateway computers without pip deps.
"""
from __future__ import annotations

import base64
import bisect
import hashlib
import hmac
import secrets
import io
import zipfile
import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import traceback
import uuid
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import urllib.request
import urllib.error

from payload_codec import (
    EMF1_FPORT, EMF1_MAGIC, EMF1_REGISTRY, FIELD_DEFINITIONS, ID_DEFAULTS,
    LEGACY_FIXED6_FPORT, WIRE_TYPES, build_codec_fields, decode_application_payload,
)

VERSION = "2.7.0-emf1-single-sketch"
HOST = os.environ.get("DASH_HOST", "0.0.0.0")
PORT = int(os.environ.get("DASH_PORT", "8026"))
COMPILER_BRIDGE_URL = os.environ.get("COMPILER_BRIDGE_URL", "http://100.93.135.116:8027/api/compile")
HISTORY_FILE = Path(os.environ.get("DASH_HISTORY_FILE", "/root/edge_monitor_state.json"))
HTML_FILE = Path(os.environ.get("DASH_HTML_FILE", "/root/index.html"))
CONFIGS_FILE = Path(os.environ.get("DASH_CONFIG_FILE", "/root/node_configs.json"))
CATALOG_FILE = Path(os.environ.get("DASH_CATALOG_FILE", str(Path(__file__).resolve().with_name("parts_catalog.json"))))
CODEC_JS_FILE = Path(os.environ.get("DASH_CODEC_JS_FILE", str(Path(__file__).resolve().with_name("chirpstack_codec.js"))))
CODEC_STANDARD_FILE = Path(os.environ.get("DASH_CODEC_STANDARD_FILE", str(Path(__file__).resolve().with_name("PAYLOAD_CODEC_STANDARD.md"))))
EMF1_REGISTRY_FILE = Path(os.environ.get("DASH_EMF1_REGISTRY_FILE", str(Path(__file__).resolve().with_name("emf1_registry.json"))))
OTA_CORE_CONTRACT_FILE = Path(os.environ.get("DASH_OTA_CORE_CONTRACT_FILE", str(Path(__file__).resolve().with_name("ota_core_contract.json"))))
CODEGEN_SCRIPT_FILE = Path(__file__).resolve().with_name("build_emf1_artifacts.py")
LATE_SECONDS = max(30, int(os.environ.get("DASH_LATE_SECONDS", "90")))
OFFLINE_SECONDS = max(LATE_SECONDS + 30, int(os.environ.get("DASH_OFFLINE_SECONDS", "180")))
SAVE_SECONDS = max(1, int(os.environ.get("DASH_SAVE_SECS", "5")))
SERIES_RETENTION_SECONDS = max(86400, int(os.environ.get("DASH_SERIES_DAYS", "7")) * 86400)
MAX_SERIES_PER_NODE = 12000
MAX_EVENTS = 600
MAX_RAW_PER_NODE = 16
MAX_SEEN = 6000
MAX_BODY = 1024 * 1024
MQTT_ENABLED = os.environ.get("DASH_MQTT_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
MQTT_HOST = os.environ.get("DASH_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("DASH_MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("DASH_MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("DASH_MQTT_PASSWORD", "")
MQTT_TOPICS = [x.strip() for x in os.environ.get(
    "DASH_MQTT_TOPIC", "application/+/device/+/event/+"
).split(",") if x.strip()]

# Optional technician login. Authentication stays disabled until an administrator
# sets DASH_AUTH_PASSWORD in /etc/default/edge-monitor. Never ship a default password.
AUTH_USERNAME = os.environ.get("DASH_AUTH_USERNAME", "technician")
AUTH_PASSWORD = os.environ.get("DASH_AUTH_PASSWORD", "")
AUTH_COOKIE_SECURE = os.environ.get("DASH_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes"}
AUTH_SESSION_SECONDS = 12 * 60 * 60
AUTH_SECRET_FILE = Path(os.environ.get("DASH_AUTH_SECRET_FILE", "/root/.edge-monitor-auth-secret"))
AUTH_SECRET = b""
if AUTH_PASSWORD:
    try:
        if AUTH_SECRET_FILE.exists():
            AUTH_SECRET = AUTH_SECRET_FILE.read_bytes()
        else:
            AUTH_SECRET = secrets.token_bytes(32)
            AUTH_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(AUTH_SECRET_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as secret_file:
                secret_file.write(AUTH_SECRET)
        if len(AUTH_SECRET) < 32:
            raise RuntimeError("auth secret must be at least 32 bytes")
    except Exception as exc:
        raise RuntimeError(f"Cannot initialize technician-auth secret at {AUTH_SECRET_FILE}: {exc}") from exc

AUTH_FAIL_LOCK = threading.Lock()
AUTH_FAILS: dict[str, list[float]] = {}
AUTH_SESSION_LOCK = threading.Lock()
AUTH_REVOKED: dict[str, float] = {}

KNOWN_NODES = {
    "d251f42dc4918f96": "ENode1",
    "b1e299ff3eed5d1d": "ENode2",
}

KNOWN_NODE_DEFAULTS = {
    "d251f42dc4918f96": {
        "joinEui": "827b193dd87983cf",
        "nwkKey": "970dd38bd89c7accb2dba04d1a429421",
        "appKey": "970dd38bd89c7accb2dba04d1a429421",
        "otaApSsid": "ECOWAN-ENode1",
        "otaApPassword": "ecowan-maintenance-ota",
        "utcSlotSecond": 5,
    },
    "b1e299ff3eed5d1d": {
        "joinEui": "6bbf280ebd665410",
        "nwkKey": "cd1dd7a9b06c626c69b463e2c1c94cbb",
        "appKey": "cd1dd7a9b06c626c69b463e2c1c94cbb",
        "otaApSsid": "ECOWAN-ENode2",
        "otaApPassword": "ecowan-maintenance-ota",
        "utcSlotSecond": 35,
    },
}

CHIRPSTACK_REST_URL = os.environ.get("DASH_CS_REST_URL", "http://127.0.0.1:8090").rstrip("/")
CHIRPSTACK_TOKEN_FILE = Path(os.environ.get("DASH_CS_TOKEN_FILE", "/root/.edge-monitor-cs-token"))
CHIRPSTACK_DEFAULT_APP_ID = os.environ.get("DASH_CS_APP_ID", "51cb9ad2-c351-4067-8baf-247555359e53")
CHIRPSTACK_DEFAULT_PROFILE_ID = os.environ.get("DASH_CS_PROFILE_ID", "dce4f2d0-8742-4a2a-aec3-fd69e8bdd658")
_CS_TOKEN_CACHE: str = ""
_GW_RADIO_CACHE: dict = {"ts": 0.0, "data": None}

LOCK = threading.RLock()
STOP = threading.Event()
PROCESS_STARTED = time.time()


def utc_now() -> float:
    return time.time()


def parse_time(value, fallback: float | None = None) -> float:
    if isinstance(value, (int, float)) and math.isfinite(value):
        # Accommodate both epoch seconds and milliseconds.
        v = float(value)
        return v / 1000.0 if v > 10_000_000_000 else v
    if isinstance(value, str) and value.strip():
        s = value.strip()
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, OverflowError):
            pass
    return utc_now() if fallback is None else fallback


def num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def int_or_none(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def default_node(eui: str, name: str, color: int = 0) -> dict:
    return {
        "eui": eui, "name": name, "color": color % 9,
        "app": None, "gateway": None, "profile": None,
        "last_ts": 0, "first_ts": 0, "last_join_ts": 0,
        "interval": 60, "late_after": LATE_SECONDS, "offline_after": OFFLINE_SECONDS,
        "temperature": None, "humidity": None, "moisture": None, "measurements": {},
        "rssi": None, "snr": None, "sf": None, "dr": None,
        "bw": None, "cr": None, "freq": None, "channel": None,
        "adr": None, "fcnt": None, "f_port": None, "payload_format": None, "dev_addr": None,
        "hex": None, "n_24h": 0, "missed_24h": 0, "n_1h": 0,
        "missed_1h": 0, "missed": 0, "resyncs": 0, "resets": 0,
        "alert": None, "decode_note": None,
        # Private bookkeeping fields are excluded from API node objects.
        "_last_fcnt": None, "_last_up_ts": 0, "_up_times": [], "_gaps": [],
    }


def fresh_state() -> dict:
    nodes = {}
    for i, (eui, name) in enumerate(KNOWN_NODES.items()):
        nodes[eui] = default_node(eui, name, i)
    return {
        "version": VERSION,
        "epoch": str(uuid.uuid4()),
        "rev": 0,
        "nodes": nodes,
        "events": [],
        "series": {eui: [] for eui in nodes},
        "raw": {eui: {} for eui in nodes},
        "seen": [],
        "counts": {"http": 0, "dup": 0, "bad": 0},
        "last_http": 0,
        "last_good": 0,
        "last_bad": None,
        "started": PROCESS_STARTED,
        "mqtt": {
            "state": "off" if not MQTT_ENABLED else "connecting",
            "host": f"{MQTT_HOST}:{MQTT_PORT}", "error": "", "topics": MQTT_TOPICS,
            "msgs": 0, "last_ts": 0, "last_topic": "", "since": utc_now(),
        },
        "save": {"error": "", "ts": 0, "bytes": 0, "file": str(HISTORY_FILE)},
        "config": {"save_secs": SAVE_SECONDS},
        "_last_save_wall": 0,
    }


def load_state() -> dict:
    state = fresh_state()
    try:
        doc = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("nodes"), dict):
            return state
        state["epoch"] = str(doc.get("epoch") or state["epoch"])
        state["rev"] = int_or_none(doc.get("rev")) or 0
        state["events"] = doc.get("events") if isinstance(doc.get("events"), list) else []
        state["events"] = state["events"][:MAX_EVENTS]
        state["counts"].update(doc.get("counts") if isinstance(doc.get("counts"), dict) else {})
        state["last_http"] = num(doc.get("last_http")) or 0
        state["last_good"] = num(doc.get("last_good")) or 0
        state["last_bad"] = doc.get("last_bad") if isinstance(doc.get("last_bad"), dict) else None
        state["seen"] = doc.get("seen") if isinstance(doc.get("seen"), list) else []
        state["seen"] = state["seen"][-MAX_SEEN:]
        saved_nodes = doc.get("nodes", {})
        for eui, saved in saved_nodes.items():
            if not isinstance(saved, dict):
                continue
            if eui not in state["nodes"]:
                state["nodes"][eui] = default_node(eui, str(saved.get("name") or "Unknown node"), len(state["nodes"]))
                state["series"][eui] = []
                state["raw"][eui] = {}
            node = state["nodes"][eui]
            for key, value in saved.items():
                if key in node or key.startswith("_"):
                    node[key] = value
            # Fill new fields introduced by a later version.
            base = default_node(eui, node.get("name") or "Unknown node", node.get("color", 0))
            for key, value in base.items():
                node.setdefault(key, value)
            node["_up_times"] = node.get("_up_times") if isinstance(node.get("_up_times"), list) else []
            node["_gaps"] = node.get("_gaps") if isinstance(node.get("_gaps"), list) else []
            node["late_after"] = LATE_SECONDS
            node["offline_after"] = OFFLINE_SECONDS
        saved_series = doc.get("series", {})
        for eui, points in saved_series.items():
            if eui in state["nodes"] and isinstance(points, list):
                cutoff = utc_now() - SERIES_RETENTION_SECONDS
                state["series"][eui] = [p for p in points if isinstance(p, list) and len(p) >= 7 and num(p[0]) is not None and float(p[0]) >= cutoff][-MAX_SERIES_PER_NODE:]
        saved_raw = doc.get("raw", {})
        for eui, raw in saved_raw.items():
            if eui in state["nodes"] and isinstance(raw, dict):
                state["raw"][eui] = raw
        # Known nodes are never lost if a saved file was incomplete.
        for eui, name in KNOWN_NODES.items():
            state["nodes"].setdefault(eui, default_node(eui, name, len(state["nodes"])))
            state["series"].setdefault(eui, [])
            state["raw"].setdefault(eui, {})
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[storage] Could not load {HISTORY_FILE}: {exc}", flush=True)
    state["started"] = PROCESS_STARTED
    state["mqtt"]["state"] = "off" if not MQTT_ENABLED else "connecting"
    state["mqtt"]["since"] = utc_now()
    return state


CONFIG_SCHEMA_VERSION = 3
NODE_CONFIGS: dict[str, dict] = {}


def load_catalog_document() -> dict:
    """Load the part/driver catalogue from a sidecar JSON file."""
    try:
        doc = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("parts"), list) and isinstance(doc.get("protocols"), list):
            return doc
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[catalog] Could not load {CATALOG_FILE}: {exc}", flush=True)
    return {"schemaVersion": 1, "catalogVersion": "missing", "protocols": [], "parts": [], "portProfiles": {}, "firmwareTemplates": {}}


def load_config_store() -> dict[str, dict]:
    try:
        doc = json.loads(CONFIGS_FILE.read_text(encoding="utf-8"))
        configs = doc.get("configs", {}) if isinstance(doc, dict) else {}
        if not isinstance(configs, dict):
            return {}
        out = {}
        for eui, config in configs.items():
            normalized = str(eui).lower().replace(":", "").replace("-", "").strip()
            if re.fullmatch(r"[0-9a-f]{16}", normalized) and isinstance(config, dict):
                ref = KNOWN_NODE_DEFAULTS.get(normalized, {})
                for k, v in ref.items():
                    if not config.get(k) and config.get(k) != 0:
                        config[k] = v
                out[normalized] = config
        return out
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[config] Could not load {CONFIGS_FILE}: {exc}", flush=True)
        return {}


def save_config_store_locked() -> None:
    payload = {"schemaVersion": CONFIG_SCHEMA_VERSION, "savedAt": utc_now(), "configs": NODE_CONFIGS}
    tmp = CONFIGS_FILE.with_name(CONFIGS_FILE.name + ".tmp")
    CONFIGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o640)
    os.replace(tmp, CONFIGS_FILE)


def config_error(message: str):
    raise ValueError(message)


def validate_node_config(doc: dict, catalog: dict) -> dict:
    """Validate and normalize a node's saved sensor map and OTAA/OTA identity before persisting it."""
    if not isinstance(doc, dict):
        config_error("Expected a JSON object")
    eui = str(doc.get("devEui") or doc.get("eui") or "").lower().replace(":", "").replace("-", "").strip()
    if not re.fullmatch(r"[0-9a-f]{16}", eui):
        config_error("DevEUI must be exactly 16 hexadecimal characters")
    ref = KNOWN_NODE_DEFAULTS.get(eui, {})
    name = str(doc.get("name") or f"Node {eui[-4:]}").strip()[:80]
    if not name:
        config_error("Node name is required")
    placement = str(doc.get("placement") or "").strip()[:240]
    notes = str(doc.get("notes") or "").strip()[:1500]
    try:
        interval = int(doc.get("reportingIntervalSeconds", 60))
    except (ValueError, TypeError, OverflowError):
        config_error("Reporting interval must be a whole number of seconds")
    if interval < 10 or interval > 86400:
        config_error("Reporting interval must be between 10 and 86400 seconds")
    raw_join = str(doc.get("joinEui") if doc.get("joinEui") is not None else (ref.get("joinEui") or "0000000000000000")).lower().replace(":", "").replace("-", "").replace(" ", "").strip()
    join_eui = raw_join if raw_join else "0000000000000000"
    if not re.fullmatch(r"[0-9a-f]{16}", join_eui):
        config_error("JoinEUI must be 16 hexadecimal characters")
    nwk_key = str(doc.get("nwkKey") if doc.get("nwkKey") is not None else (ref.get("nwkKey") or "")).lower().replace(":", "").replace("-", "").replace(" ", "").strip()
    app_key = str(doc.get("appKey") if doc.get("appKey") is not None else (ref.get("appKey") or "")).lower().replace(":", "").replace("-", "").replace(" ", "").strip()
    if nwk_key and not app_key:
        app_key = nwk_key
    elif app_key and not nwk_key:
        nwk_key = app_key
    if nwk_key and not re.fullmatch(r"[0-9a-f]{32}", nwk_key):
        config_error("NwkKey must be 32 hexadecimal characters (16 bytes)")
    if app_key and not re.fullmatch(r"[0-9a-f]{32}", app_key):
        config_error("AppKey must be 32 hexadecimal characters (16 bytes)")
    default_ssid = ref.get("otaApSsid") or f"ECOWAN-{re.sub(r'[^A-Za-z0-9_-]+', '', name)[:24] or eui[-4:]}"
    ota_ssid = str(doc.get("otaApSsid") or doc.get("otaSsid") or default_ssid).strip()[:32]
    ota_pass = str(doc.get("otaApPassword") or doc.get("otaPassword") or ref.get("otaApPassword") or "ecowan-maintenance-ota").strip()[:63]
    if len(ota_pass) < 8:
        config_error("WLAN OTA AP Password must be at least 8 characters")
    try:
        raw_slot = doc.get("utcSlotSecond")
        utc_slot = int(raw_slot) if raw_slot not in (None, "") else int(ref.get("utcSlotSecond", 5))
    except (ValueError, TypeError, OverflowError):
        utc_slot = int(ref.get("utcSlotSecond", 5))
    utc_slot = max(0, min(interval - 1, utc_slot))
    profile = str(doc.get("firmwareProfile") or "esp32_lorawan").strip()[:80]
    requested_codec = str(doc.get("payloadCodec") or "edge_measurement_v1").strip()
    if requested_codec != "edge_measurement_v1":
        if requested_codec == "legacy_fixed6_v1":
            config_error("The code generator is EMF1-only. Legacy Fixed6 is receive-only compatibility and cannot be generated.")
        config_error("Payload codec must be edge_measurement_v1 (EMF1 v1)")
    payload_codec = "edge_measurement_v1"
    sensors_in = doc.get("sensors", [])
    if not isinstance(sensors_in, list) or len(sensors_in) > 32:
        config_error("Sensors must be a list with no more than 32 modules")
    catalog_by_id = {str(p.get("id")): p for p in catalog.get("parts", []) if isinstance(p, dict)}
    protocol_ids = {str(p.get("id")) for p in catalog.get("protocols", []) if isinstance(p, dict)}
    cleaned_sensors = []
    for idx, sensor in enumerate(sensors_in):
        if not isinstance(sensor, dict):
            config_error(f"Sensor {idx + 1} is not an object")
        part_id = str(sensor.get("partId") or sensor.get("sensorId") or "custom_module")[:100]
        part = catalog_by_id.get(part_id)
        if not part:
            config_error(f"Unknown part id: {part_id}")
        if part.get("wizardSelectable") is False:
            config_error(f"{part.get('name', part_id)} is not a sensor/module option in this wizard")
        protocol = str(sensor.get("protocol") or (part.get("protocols") or ["custom"])[0])
        if protocol not in protocol_ids:
            config_error(f"Unsupported protocol id: {protocol}")
        if protocol not in part.get("protocols", []):
            config_error(f"Protocol {protocol} is not listed for {part.get('name', part_id)}")
        fields_in = sensor.get("fields", [])
        if not isinstance(fields_in, list) or len(fields_in) > 64:
            config_error(f"Sensor {idx + 1} fields must be a list of at most 64 items")
        fields = []
        for field in fields_in:
            if not isinstance(field, dict):
                continue
            key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(field.get("key") or "").strip())[:64]
            if not key:
                continue
            definition = FIELD_DEFINITIONS.get(key)
            raw_wire_type = field.get("wireType", field.get("typeCode"))
            if raw_wire_type in (None, ""):
                if definition:
                    wire_type = int(definition["typeCode"])
                else:
                    config_error(f"Custom measurement field {key!r} must specify an EMF1 wireType.")
            else:
                try:
                    wire_type = int(raw_wire_type)
                except (TypeError, ValueError):
                    config_error(f"Measurement field {key!r} has an invalid wireType.")
            if wire_type not in WIRE_TYPES:
                config_error(f"Measurement field {key!r} uses unsupported EMF1 wire type {raw_wire_type!r}.")
            if definition and wire_type != int(definition["typeCode"]):
                config_error(f"Common field {key!r} must use its registry type 0x{int(definition['typeCode']):02X}.")
            fields.append({
                "key": key,
                "label": str(field.get("label") or key)[:80],
                "unit": str(field.get("unit") or "")[:24],
                "wireType": wire_type,
            })
        cleaned_sensors.append({
            "id": str(sensor.get("id") or uuid.uuid4())[:80],
            "partId": part_id,
            "label": str(sensor.get("label") or part.get("name") or part_id).strip()[:100],
            "protocol": protocol,
            "port": str(sensor.get("port") or "CUSTOM_PORT")[:100],
            "busAddress": str(sensor.get("busAddress") or "").strip()[:80],
            "notes": str(sensor.get("notes") or "").strip()[:500],
            "compatibility": part.get("compatibility", "interface_required"),
            "fields": fields,
        })
    codec_fields = build_codec_fields(cleaned_sensors, doc.get("codecFields") if isinstance(doc.get("codecFields"), list) else None)
    now = utc_now()
    codec_f_port = EMF1_FPORT
    return {
        "schemaVersion": CONFIG_SCHEMA_VERSION,
        "devEui": eui,
        "joinEui": join_eui,
        "nwkKey": nwk_key,
        "appKey": app_key,
        "otaApSsid": ota_ssid,
        "otaApPassword": ota_pass,
        "utcSlotSecond": utc_slot,
        "name": name,
        "placement": placement,
        "reportingIntervalSeconds": interval,
        "firmwareProfile": profile,
        "payloadCodec": payload_codec,
        "payloadCodecVersion": "EMF1",
        "codecFPort": codec_f_port,
        "codecRegistrySchema": str(EMF1_REGISTRY.get("schema")),
        "codecFields": codec_fields,
        "notes": notes,
        "sensors": cleaned_sensors,
        "updatedAt": now,
        "catalogVersion": str(catalog.get("catalogVersion") or "unknown"),
    }


def cs_get_or_create_token() -> str:
    """Return a cached or auto-generated ChirpStack v4 local REST API token."""
    global _CS_TOKEN_CACHE
    if _CS_TOKEN_CACHE:
        return _CS_TOKEN_CACHE
    env_tok = os.environ.get("DASH_CS_API_TOKEN", "").strip()
    if env_tok:
        _CS_TOKEN_CACHE = env_tok
        return _CS_TOKEN_CACHE
    try:
        if CHIRPSTACK_TOKEN_FILE.exists():
            tok = CHIRPSTACK_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if tok:
                _CS_TOKEN_CACHE = tok
                return _CS_TOKEN_CACHE
    except Exception:
        pass
    for cmd in (
        ["docker", "exec", "chirpstack_chirpstack_1", "chirpstack", "-c", "/etc/chirpstack", "create-api-key", "--name", "edge-monitor-auto"],
        ["chirpstack", "-c", "/etc/chirpstack", "create-api-key", "--name", "edge-monitor-auto"],
    ):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
            if res.returncode == 0 and res.stdout:
                m = re.search(r"token:\s*(\S+)", res.stdout)
                if m:
                    tok = m.group(1).strip()
                    _CS_TOKEN_CACHE = tok
                    try:
                        CHIRPSTACK_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                        CHIRPSTACK_TOKEN_FILE.write_text(tok + "\n", encoding="utf-8")
                        os.chmod(CHIRPSTACK_TOKEN_FILE, 0o600)
                    except Exception:
                        pass
                    return _CS_TOKEN_CACHE
        except Exception:
            continue
    return ""


def cs_api_request(method: str, path: str, body: dict | None = None, timeout: float = 5.0) -> tuple[int, dict]:
    """Perform a request against the local ChirpStack v4 REST API (127.0.0.1:8090)."""
    token = cs_get_or_create_token()
    if not token:
        return 0, {"error": "ChirpStack API token unavailable"}
    url = f"{CHIRPSTACK_REST_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/json",
        "Grpc-Metadata-Authorization": f"Bearer {token}",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            return exc.code, (json.loads(raw) if raw.strip() else {"error": f"HTTP {exc.code}"})
        except Exception:
            return exc.code, {"error": f"HTTP {exc.code}"}
    except Exception as exc:
        return 0, {"error": str(exc)}


def cs_resolve_ids() -> tuple[str, str]:
    """Return (applicationId, deviceProfileId) for provisioning nodes in ChirpStack."""
    app_id = CHIRPSTACK_DEFAULT_APP_ID
    prof_id = CHIRPSTACK_DEFAULT_PROFILE_ID
    status, tenants = cs_api_request("GET", "/api/tenants?limit=5")
    tenant_id = "52f14cd4-c6f1-4fbd-8f87-4025e1d49242"
    if status == 200 and isinstance(tenants.get("result"), list) and tenants["result"]:
        tenant_id = str(tenants["result"][0].get("id") or tenant_id)
    status_a, apps = cs_api_request("GET", f"/api/applications?limit=10&tenantId={tenant_id}")
    if status_a == 200 and isinstance(apps.get("result"), list) and apps["result"]:
        app_id = str(apps["result"][0].get("id") or app_id)
    status_p, profs = cs_api_request("GET", f"/api/device-profiles?limit=10&tenantId={tenant_id}")
    if status_p == 200 and isinstance(profs.get("result"), list) and profs["result"]:
        prof_id = str(profs["result"][0].get("id") or prof_id)
    return app_id, prof_id


def chirpstack_sync_device(cfg: dict) -> dict:
    """Automatically create or update the device and its OTAA keys in ChirpStack."""
    eui = str(cfg.get("devEui") or "").lower().strip()
    if not re.fullmatch(r"[0-9a-f]{16}", eui):
        return {"ok": False, "error": "Invalid DevEUI"}
    status_get, existing = cs_api_request("GET", f"/api/devices/{eui}")
    if status_get == 0:
        return {"ok": False, "skipped": True, "reason": existing.get("error", "ChirpStack REST API unreachable")}
    if status_get == 200 and isinstance(existing.get("device"), dict):
        ex_dev = existing["device"]
        app_id = str(ex_dev.get("applicationId") or CHIRPSTACK_DEFAULT_APP_ID)
        prof_id = str(ex_dev.get("deviceProfileId") or CHIRPSTACK_DEFAULT_PROFILE_ID)
        device_obj = {
            "applicationId": app_id,
            "deviceProfileId": prof_id,
            "devEui": eui,
            "name": str(cfg.get("name") or ex_dev.get("name") or f"Node {eui[-4:]}"),
            "description": str(cfg.get("placement") or ex_dev.get("description") or ""),
            "joinEui": str(cfg.get("joinEui") or ex_dev.get("joinEui") or "0000000000000000"),
            "isDisabled": False,
            "skipFcntCheck": bool(ex_dev.get("skipFcntCheck", False)),
            "tags": ex_dev.get("tags") if isinstance(ex_dev.get("tags"), dict) else {},
            "variables": ex_dev.get("variables") if isinstance(ex_dev.get("variables"), dict) else {},
        }
        st_dev, resp_dev = cs_api_request("PUT", f"/api/devices/{eui}", {"device": device_obj})
        action = "updated"
    elif status_get == 404:
        app_id, prof_id = cs_resolve_ids()
        device_obj = {
            "applicationId": app_id,
            "deviceProfileId": prof_id,
            "devEui": eui,
            "name": str(cfg.get("name") or f"Node {eui[-4:]}"),
            "description": str(cfg.get("placement") or ""),
            "joinEui": str(cfg.get("joinEui") or "0000000000000000"),
            "isDisabled": False,
            "skipFcntCheck": False,
            "tags": {},
            "variables": {},
        }
        st_dev, resp_dev = cs_api_request("POST", "/api/devices", {"device": device_obj})
        action = "created"
    else:
        return {"ok": False, "error": resp_error_text(existing, f"ChirpStack GET device returned HTTP {status_get}")}

    if st_dev not in (200, 201):
        return {"ok": False, "error": resp_error_text(resp_dev, f"ChirpStack device {action} failed (HTTP {st_dev})")}

    nwk_key = str(cfg.get("nwkKey") or "").lower().strip()
    app_key = str(cfg.get("appKey") or nwk_key).lower().strip()
    keys_updated = False
    if re.fullmatch(r"[0-9a-f]{32}", nwk_key) and nwk_key != ("0" * 32):
        if not re.fullmatch(r"[0-9a-f]{32}", app_key) or app_key == ("0" * 32):
            app_key = nwk_key
        st_kget, _ = cs_api_request("GET", f"/api/devices/{eui}/keys")
        keys_body = {"deviceKeys": {"devEui": eui, "nwkKey": nwk_key, "appKey": app_key}}
        if st_kget == 200:
            st_k, resp_k = cs_api_request("PUT", f"/api/devices/{eui}/keys", keys_body)
        else:
            st_k, resp_k = cs_api_request("POST", f"/api/devices/{eui}/keys", keys_body)
        if st_k in (200, 201):
            keys_updated = True
        else:
            return {"ok": False, "action": action, "error": resp_error_text(resp_k, f"Device {action}, but setting OTAA keys failed (HTTP {st_k})")}
    return {"ok": True, "action": action, "keysUpdated": keys_updated, "devEui": eui}


def resp_error_text(resp: dict, fallback: str) -> str:
    if isinstance(resp, dict):
        msg = resp.get("message") or resp.get("error")
        if msg:
            return str(msg)
    return fallback


def chirpstack_reset_join(eui: str) -> dict:
    """Clear stored DevNonces, active session, and downlink queue in ChirpStack so a node can re-join."""
    eui = str(eui or "").lower().replace(":", "").replace("-", "").strip()
    if not re.fullmatch(r"[0-9a-f]{16}", eui):
        return {"ok": False, "error": "DevEUI must be 16 hexadecimal characters"}
    st_n, resp_n = cs_api_request("DELETE", f"/api/devices/{eui}/dev-nonces")
    if st_n == 0:
        return {"ok": False, "error": resp_error_text(resp_n, "ChirpStack REST API unreachable")}
    if st_n == 404:
        return {"ok": False, "error": f"Device {eui} is not registered in the gateway radio server yet. Save Configuration first."}
    st_a, _ = cs_api_request("DELETE", f"/api/devices/{eui}/activation")
    st_q, _ = cs_api_request("DELETE", f"/api/devices/{eui}/queue")
    with LOCK:
        node = STATE["nodes"].get(eui)
        if node:
            node["_last_fcnt"] = None
            node["alert"] = None
            append_event_locked(
                eui, "other", "info", utc_now(),
                {"description": "Reset LoRaWAN join nonces & active session from Edge Monitor"},
                node, "DASH",
            )
            STATE["rev"] += 1
            persist_locked(force=True)
    return {
        "ok": True,
        "devEui": eui,
        "noncesCleared": st_n == 200,
        "sessionFlushed": st_a in (200, 404),
        "queueFlushed": st_q in (200, 404),
    }


def chirpstack_delete_device(eui: str) -> dict:
    """Delete a device from ChirpStack and remove it from Edge Monitor."""
    eui = str(eui or "").lower().replace(":", "").replace("-", "").strip()
    if not re.fullmatch(r"[0-9a-f]{16}", eui):
        return {"ok": False, "error": "DevEUI must be 16 hexadecimal characters"}
    st_del, _ = cs_api_request("DELETE", f"/api/devices/{eui}")
    return {"ok": True, "devEui": eui, "chirpstackDeleted": st_del in (200, 404)}


def get_cached_gateway_radio() -> dict:
    """Return cached Dragino SX1302 packet forwarder + ChirpStack gateway status."""
    now = utc_now()
    if _GW_RADIO_CACHE.get("data") and (now - float(_GW_RADIO_CACHE.get("ts") or 0)) < 10.0:
        return _GW_RADIO_CACHE["data"]
    iot_status = ""
    try:
        p = Path("/var/iot/status")
        if p.exists():
            iot_status = p.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        pass
    fwd_running = False
    try:
        res = subprocess.run(["ps", "-ef"], capture_output=True, text=True, timeout=2)
        if res.returncode == 0 and "/usr/bin/fwd" in res.stdout:
            fwd_running = True
    except Exception:
        pass
    gw_info = {
        "ok": fwd_running or iot_status.lower() == "online",
        "gatewayId": "a84041fdfe257d3b",
        "name": "Dragino-LPS8v2",
        "region": "AS923",
        "fwdRunning": fwd_running,
        "iotStatus": iot_status or ("online" if fwd_running else "unknown"),
        "csState": "UNKNOWN",
        "lastSeenTs": 0.0,
        "csApi": "offline",
    }
    st_gw, gws = cs_api_request("GET", "/api/gateways?limit=5", timeout=2.0)
    if st_gw == 200 and isinstance(gws.get("result"), list):
        gw_info["csApi"] = "connected"
        if gws["result"]:
            g0 = gws["result"][0]
            gw_info["gatewayId"] = str(g0.get("gatewayId") or gw_info["gatewayId"])
            gw_info["name"] = str(g0.get("name") or gw_info["name"])
            gw_info["csState"] = str(g0.get("state") or "ONLINE")
            if g0.get("lastSeenAt"):
                gw_info["lastSeenTs"] = parse_time(g0.get("lastSeenAt"), 0.0)
            gw_info["ok"] = gw_info["ok"] or gw_info["csState"] == "ONLINE"
    _GW_RADIO_CACHE["ts"] = now
    _GW_RADIO_CACHE["data"] = gw_info
    return gw_info


STATE = load_state()
PART_CATALOG = load_catalog_document()
NODE_CONFIGS = load_config_store()
with LOCK:
    for configured_eui, cfg in NODE_CONFIGS.items():
        if configured_eui not in STATE["nodes"]:
            STATE["nodes"][configured_eui] = default_node(configured_eui, cfg.get("name") or "Configured node", len(STATE["nodes"]))
            STATE["series"].setdefault(configured_eui, [])
            STATE["raw"].setdefault(configured_eui, {})
        STATE["nodes"][configured_eui]["name"] = cfg.get("name") or STATE["nodes"][configured_eui].get("name")


def persist_locked(force: bool = False) -> None:
    now = utc_now()
    if not force and now - STATE.get("_last_save_wall", 0) < SAVE_SECONDS:
        return
    payload = {
        "version": VERSION, "epoch": STATE["epoch"], "rev": STATE["rev"],
        "nodes": STATE["nodes"], "events": STATE["events"],
        "series": STATE["series"], "raw": STATE["raw"], "seen": STATE["seen"],
        "counts": STATE["counts"], "last_http": STATE["last_http"],
        "last_good": STATE["last_good"], "last_bad": STATE["last_bad"],
    }
    tmp = HISTORY_FILE.with_name(HISTORY_FILE.name + ".tmp")
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with open(tmp, "wb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, HISTORY_FILE)
        STATE["_last_save_wall"] = now
        STATE["save"] = {"error": "", "ts": now, "bytes": len(encoded), "file": str(HISTORY_FILE)}
    except Exception as exc:
        STATE["save"] = {"error": str(exc), "ts": STATE.get("save", {}).get("ts", 0),
                         "bytes": STATE.get("save", {}).get("bytes", 0), "file": str(HISTORY_FILE)}
        print(f"[storage] Save failed: {exc}", flush=True)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def ensure_node_locked(eui: str, name: str | None, device_info: dict) -> dict:
    if eui not in STATE["nodes"]:
        STATE["nodes"][eui] = default_node(eui, name or KNOWN_NODES.get(eui, "Unknown node"), len(STATE["nodes"]))
        STATE["series"].setdefault(eui, [])
        STATE["raw"].setdefault(eui, {})
    node = STATE["nodes"][eui]
    if name and (not node.get("name") or node["name"] == "Unknown node"):
        node["name"] = name
    if device_info.get("applicationName"):
        node["app"] = str(device_info["applicationName"])
    if device_info.get("deviceProfileName"):
        node["profile"] = str(device_info["deviceProfileName"])
    return node


def get_topic_bits(topic: str) -> tuple[str | None, str | None]:
    # Default ChirpStack v4 topic is application/<id>/device/<eui>/event/<type>.
    # Search the segment names rather than hard-coding the first segment, so a
    # customized prefix can still carry through EUI and event type.
    parts = (topic or "").strip("/").split("/")
    eui, kind = None, None
    for i, part in enumerate(parts[:-1]):
        if part == "device" and i + 1 < len(parts):
            eui = parts[i + 1].lower()
        elif part == "event" and i + 1 < len(parts):
            kind = parts[i + 1].lower()
    return eui, kind


def event_level(event_type: str, payload: dict) -> str:
    if event_type == "log":
        level = str(payload.get("level") or "INFO").lower()
        if "error" in level or "critical" in level or "fatal" in level:
            return "error"
        if "warn" in level:
            return "warn"
    if event_type == "up":
        return "info"
    if event_type in {"join", "status", "ack", "txack"}:
        if event_type in {"ack", "txack"} and payload.get("acknowledged") is False:
            return "warn"
        return "info"
    return "info"


def dedup_key(payload: dict, event_type: str, eui: str) -> str:
    did = payload.get("deduplicationId") or payload.get("deduplication_id")
    if did:
        return f"id:{event_type}:{str(did)}"
    # The complete canonical payload is a fallback; this keeps a webhook + MQTT
    # copy from being counted twice even when a test/event omits deduplicationId.
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except Exception:
        canonical = repr(payload)
    return hashlib.sha256((event_type + "|" + eui + "|" + canonical).encode("utf-8", "replace")).hexdigest()


def decode_payload(payload: dict) -> tuple[bytes, str | None]:
    data = payload.get("data")
    if not data:
        return b"", None
    try:
        raw = base64.b64decode(str(data), validate=True)
        return raw, None
    except Exception as exc:
        return b"", f"Invalid base64 payload: {exc}"


def finite_or_none(value):
    return num(value)


def trim_old_locked(now: float) -> None:
    cutoff = now - SERIES_RETENTION_SECONDS
    for eui, points in STATE["series"].items():
        j = 0
        while j < len(points) and points[j][0] < cutoff:
            j += 1
        if j:
            del points[:j]
        if len(points) > MAX_SERIES_PER_NODE:
            del points[:-MAX_SERIES_PER_NODE]
    for node in STATE["nodes"].values():
        node["_up_times"] = [t for t in node.get("_up_times", []) if num(t) is not None and t >= cutoff]
        node["_gaps"] = [g for g in node.get("_gaps", []) if num(g.get("ts")) is not None and g["ts"] >= now - 86400 * 7]


def append_event_locked(eui: str | None, event_type: str, level: str, ts: float,
                        payload: dict, node: dict | None, via: str) -> None:
    name = node.get("name") if node else (str((payload.get("deviceInfo") or {}).get("deviceName") or "ChirpStack"))
    d = {}
    if event_type == "up":
        d = {
            "t": node.get("temperature") if node else None,
            "h": node.get("humidity") if node else None,
            "m": node.get("moisture") if node else None,
            "fcnt": int_or_none(payload.get("fCnt")),
            "rssi": node.get("rssi") if node else None,
            "snr": node.get("snr") if node else None,
            "via": via,
        }
    elif event_type == "join":
        d = {"dev_addr": payload.get("devAddr") or payload.get("dev_addr")}
    elif event_type == "log":
        d = {
            "text": payload.get("description") or payload.get("message") or payload.get("error") or "ChirpStack log event",
            "level": payload.get("level", level),
            "code": payload.get("code") or payload.get("errorCode") or payload.get("error_code"),
        }
    elif event_type == "status":
        d = {"battery": payload.get("batteryLevel", payload.get("battery")),
             "margin": payload.get("margin")}
    else:
        d = {"text": payload.get("description") or payload.get("message") or payload.get("error") or ""}
    STATE["events"].insert(0, {
        "ts": ts, "type": event_type, "level": level, "eui": eui or "",
        "name": name or "Unknown", "d": d,
    })
    del STATE["events"][MAX_EVENTS:]


def ingest_message(raw_text: str, event_type: str | None, source: str,
                   topic: str = "") -> tuple[bool, str]:
    """Parse and record one event. Returns (accepted, short_reason)."""
    received_ts = utc_now()
    if source == "HTTP":
        with LOCK:
            STATE["counts"]["http"] = int(STATE["counts"].get("http", 0)) + 1
            STATE["last_http"] = received_ts
    else:
        with LOCK:
            m = STATE["mqtt"]
            m["msgs"] = int(m.get("msgs", 0)) + 1
            m["last_ts"] = received_ts
            if topic:
                m["last_topic"] = topic
            if m.get("state") != "connected":
                m["state"] = "connected"
                m["error"] = ""
                m["since"] = received_ts

    try:
        payload = json.loads(raw_text)
        if not isinstance(payload, dict):
            raise ValueError("JSON root is not an object")
    except Exception as exc:
        with LOCK:
            STATE["counts"]["bad"] = int(STATE["counts"].get("bad", 0)) + 1
            STATE["last_bad"] = {"ts": received_ts, "error": f"JSON parse failed: {exc}", "sample": raw_text[:300]}
            STATE["rev"] += 1
            persist_locked()
        return False, "invalid JSON"

    with LOCK:
        STATE["last_good"] = received_ts
    topic_eui, topic_type = get_topic_bits(topic)
    dev_info = payload.get("deviceInfo") or payload.get("device_info") or {}
    if not isinstance(dev_info, dict):
        dev_info = {}
    eui = str(dev_info.get("devEui") or dev_info.get("devEUI") or payload.get("devEui") or payload.get("devEUI") or topic_eui or "").lower()
    eui = eui.replace(":", "").replace("-", "").strip()
    et = str(event_type or topic_type or "").lower()
    if not et:
        if "data" in payload or "object" in payload or "fCnt" in payload or "fPort" in payload:
            et = "up"
        else:
            et = "other"
    if et not in {"up", "join", "log", "status", "ack", "txack", "location", "integration"}:
        et = "other"

    # An application-level integration event may not contain a device EUI.
    # Keep its health/counts without inventing a device row.
    if not eui:
        with LOCK:
            if et == "other" or et == "integration":
                STATE["rev"] += 1
                append_event_locked(None, et, event_level(et, payload), parse_time(payload.get("time"), received_ts), payload, None, source)
                STATE["raw"].setdefault("_system", {})[et] = {
                    "received_at": received_ts, "source": source, "topic": topic, "payload": payload,
                }
                persist_locked()
                return True, "system event"
        return False, "event has no device EUI"

    name = str(dev_info.get("deviceName") or KNOWN_NODES.get(eui) or f"Node {eui[-4:]}")
    fingerprint = dedup_key(payload, et, eui)
    raw_bytes, decode_error = decode_payload(payload) if et == "up" else (b"", None)
    raw_hex = raw_bytes.hex(" ").upper() if raw_bytes else ""
    obj = payload.get("object") if isinstance(payload.get("object"), dict) else {}
    obj_t = finite_or_none(obj.get("temperature"))
    obj_h = finite_or_none(obj.get("humidity"))
    obj_m = finite_or_none(obj.get("moisture"))
    f_port = int_or_none(payload.get("fPort", payload.get("f_port")))
    node_cfg_for_codec = NODE_CONFIGS.get(eui, {})
    codec_fields = node_cfg_for_codec.get("codecFields")
    if not isinstance(codec_fields, list):
        codec_fields = build_codec_fields(node_cfg_for_codec.get("sensors", []))
    payload_format, wire_measurements, wire_error = decode_application_payload(raw_bytes, f_port, codec_fields) if et == "up" else (None, {}, None)
    legacy_mode = payload_format == "legacy_fixed6_v1"
    byte_t = wire_measurements.get("temperature") if legacy_mode else None
    byte_h = wire_measurements.get("humidity") if legacy_mode else None
    byte_m = wire_measurements.get("moisture") if legacy_mode else None

    # Legacy Fixed6 uses -32768/65535 as invalid-DHT markers. EMF1 has typed fields
    # and does not use those legacy sentinels.
    sentinel_temp = legacy_mode and len(raw_bytes) >= 2 and int.from_bytes(raw_bytes[0:2], "big", signed=True) == -32768
    sentinel_hum = legacy_mode and len(raw_bytes) >= 4 and int.from_bytes(raw_bytes[2:4], "big", signed=False) == 65535
    temp = wire_measurements.get("temperature", obj_t if obj_t is not None else byte_t)
    hum = wire_measurements.get("humidity", obj_h if obj_h is not None else byte_h)
    moist = wire_measurements.get("moisture", obj_m if obj_m is not None else byte_m)
    if sentinel_temp:
        temp = None
    if sentinel_hum:
        hum = None
    decode_note = wire_error or decode_error
    if not decode_note and et == "up" and raw_bytes and obj:
        mismatches = []
        if obj_t is not None and "temperature" in wire_measurements and temp is not None and abs(obj_t - float(temp)) > 0.2:
            mismatches.append(f"codec temperature {obj_t:g} differs from payload value {float(temp):g}")
        if obj_h is not None and "humidity" in wire_measurements and hum is not None and abs(obj_h - float(hum)) > 0.5:
            mismatches.append(f"codec humidity {obj_h:g} differs from payload value {float(hum):g}")
        if obj_m is not None and "moisture" in wire_measurements and moist is not None and abs(obj_m - float(moist)) > 1:
            mismatches.append(f"codec moisture {obj_m:g} differs from payload value {float(moist):g}")
        if mismatches:
            decode_note = "; ".join(mismatches)

    # Preserve all scalar measurements exposed by the ChirpStack decoder, not only T/H/moisture.
    # The node's saved configuration tells the UI which keys/units should be displayed.
    decoded_measurements = {}
    for measurement_key, measurement_value in obj.items():
        safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(measurement_key).strip())[:64]
        if not safe_key:
            continue
        if isinstance(measurement_value, bool) or measurement_value is None:
            decoded_measurements[safe_key] = measurement_value
        elif isinstance(measurement_value, (int, float)) and math.isfinite(float(measurement_value)):
            decoded_measurements[safe_key] = measurement_value
        elif isinstance(measurement_value, str):
            decoded_measurements[safe_key] = measurement_value[:160]
    if et == "up":
        # Raw-byte EMF1/legacy decoding is authoritative; keep other measurements exposed by ChirpStack.
        decoded_measurements.update(wire_measurements)
        if "temperature" in wire_measurements or obj_t is not None or legacy_mode:
            decoded_measurements["temperature"] = temp
        if "humidity" in wire_measurements or obj_h is not None or legacy_mode:
            decoded_measurements["humidity"] = hum
        if "moisture" in wire_measurements or obj_m is not None or legacy_mode:
            decoded_measurements["moisture"] = moist

    rx_list = payload.get("rxInfo") or payload.get("rx_info") or []
    if not isinstance(rx_list, list):
        rx_list = []
    valid_rx = [r for r in rx_list if isinstance(r, dict)]
    # Pick the receiver with the strongest RSSI rather than assuming element 0 is best.
    chosen_rx = max(valid_rx, key=lambda r: num(r.get("rssi")) if num(r.get("rssi")) is not None else -9999, default={})
    rssi = num(chosen_rx.get("rssi"))
    snr = num(chosen_rx.get("snr"))
    gateway = chosen_rx.get("gatewayId") or chosen_rx.get("gateway_id")
    tx = payload.get("txInfo") or payload.get("tx_info") or {}
    tx = tx if isinstance(tx, dict) else {}
    modulation = tx.get("modulation") or {}
    modulation = modulation if isinstance(modulation, dict) else {}
    lora = modulation.get("lora") or {}
    lora = lora if isinstance(lora, dict) else {}
    freq = int_or_none(tx.get("frequency"))
    event_ts = parse_time(payload.get("time"), received_ts)
    if et == "up" and not payload.get("time"):
        # Older logs sometimes only timestamp the reception metadata.
        times = [r.get("time") for r in valid_rx if r.get("time")]
        if times:
            event_ts = parse_time(times[0], received_ts)
    fcnt = int_or_none(payload.get("fCnt", payload.get("f_cnt")))

    with LOCK:
        # Event ID deduplication joins MQTT + HTTP routes and catches repeat test posts.
        seen = STATE["seen"]
        if fingerprint in seen:
            STATE["counts"]["dup"] = int(STATE["counts"].get("dup", 0)) + 1
            STATE["rev"] += 1
            persist_locked()
            return False, "duplicate event suppressed"
        seen.append(fingerprint)
        if len(seen) > MAX_SEEN:
            del seen[:-MAX_SEEN]

        node = ensure_node_locked(eui, name, dev_info)
        STATE["series"].setdefault(eui, [])
        STATE["raw"].setdefault(eui, {})
        raw_store = STATE["raw"][eui]
        raw_store[et] = {"received_at": received_ts, "event_time": event_ts,
                         "source": source, "topic": topic, "payload": payload}
        # Bound the types stored per node, preserving most recently updated entries.
        while len(raw_store) > MAX_RAW_PER_NODE:
            oldest = min(raw_store, key=lambda k: raw_store[k].get("received_at", 0))
            del raw_store[oldest]

        level = event_level(et, payload)
        if et == "join":
            node["last_join_ts"] = max(float(node.get("last_join_ts") or 0), event_ts)
            node["dev_addr"] = payload.get("devAddr") or payload.get("dev_addr") or node.get("dev_addr")
            # A successful new join starts a new frame-counter sequence.
            node["_last_fcnt"] = None
            node["alert"] = None
        elif et == "log":
            code = payload.get("code") or payload.get("errorCode") or payload.get("error_code")
            text = payload.get("description") or payload.get("message") or payload.get("error") or "ChirpStack log event"
            if level in {"error", "warn"}:
                node["alert"] = {"ts": event_ts, "level": "ERROR" if level == "error" else "WARN",
                                  "code": str(code) if code is not None else "", "text": str(text)}
        elif et == "up":
            # A frame counter that is already current is normally a retransmission or replay.
            last_counter = int_or_none(node.get("_last_fcnt"))
            last_up_ts = num(node.get("_last_up_ts")) or 0
            chronological = event_ts >= last_up_ts
            if chronological and fcnt is not None and last_counter is not None:
                if fcnt == last_counter:
                    STATE["counts"]["dup"] = int(STATE["counts"].get("dup", 0)) + 1
                    STATE["rev"] += 1
                    append_event_locked(eui, "log", "warn", event_ts,
                                        {"level": "WARN", "code": "RETRANSMISSION", "description": f"Frame counter {fcnt} repeated; sensor snapshot was not overwritten."},
                                        node, source)
                    persist_locked()
                    return False, "frame-counter duplicate suppressed"
                if fcnt > last_counter + 1:
                    gap = fcnt - last_counter - 1
                    node["missed"] = int(node.get("missed", 0)) + gap
                    node.setdefault("_gaps", []).append({"ts": event_ts, "count": gap})
                elif fcnt < last_counter:
                    # Small backwards movement is likely an old/out-of-order packet;
                    # a low counter after a high one is treated as a reset.
                    if last_counter > 10 and fcnt <= 2:
                        node["resets"] = int(node.get("resets", 0)) + 1
                        node["alert"] = {"ts": event_ts, "level": "WARN", "code": "F_CNT_RESET",
                                          "text": f"Frame counter moved from {last_counter} to {fcnt}; verify session persistence."}
                    else:
                        level = "warn"
            # Series are sorted by event timestamp so old log replays do not distort current values.
            values = [event_ts, temp, hum, moist, rssi, snr, fcnt, decoded_measurements]
            series = STATE["series"][eui]
            insert_at = bisect.bisect_left([p[0] for p in series], event_ts)
            if insert_at < len(series) and abs(series[insert_at][0] - event_ts) < 0.0001:
                # Preserve one chart point per exact timestamp; FCnt makes a tie stable.
                values[0] = event_ts + 0.001
                insert_at = bisect.bisect_left([p[0] for p in series], values[0])
            series.insert(insert_at, values)
            if len(series) > MAX_SERIES_PER_NODE:
                del series[:len(series) - MAX_SERIES_PER_NODE]
            if not node.get("first_ts") or event_ts < node["first_ts"]:
                node["first_ts"] = event_ts
            if event_ts >= last_up_ts:
                if last_up_ts and event_ts > last_up_ts:
                    ups = node.setdefault("_up_times", [])
                    ups.append(event_ts)
                    deltas = [b - a for a, b in zip(ups[-8:-1], ups[-7:]) if b > a]
                    if deltas:
                        deltas.sort()
                        node["interval"] = max(10, min(86400, float(deltas[len(deltas) // 2])))
                elif not node.get("_up_times"):
                    node["_up_times"] = [event_ts]
                node["_last_up_ts"] = event_ts
                if fcnt is not None:
                    node["_last_fcnt"] = fcnt
                node["last_ts"] = event_ts
                node["name"] = NODE_CONFIGS.get(eui, {}).get("name") or name or node.get("name")
                node["dev_addr"] = payload.get("devAddr") or payload.get("dev_addr") or node.get("dev_addr")
                node["fcnt"] = fcnt
                node["f_port"] = f_port
                node["payload_format"] = payload_format
                node["dr"] = int_or_none(payload.get("dr"))
                node["adr"] = payload.get("adr") if isinstance(payload.get("adr"), bool) else None
                node["temperature"] = temp
                node["humidity"] = hum
                node["moisture"] = moist
                node["measurements"] = decoded_measurements
                node["rssi"] = rssi
                node["snr"] = snr
                node["sf"] = int_or_none(lora.get("spreadingFactor"))
                node["bw"] = int_or_none(lora.get("bandwidth"))
                node["cr"] = lora.get("codeRate")
                node["freq"] = freq
                node["channel"] = int_or_none(chosen_rx.get("channel"))
                node["gateway"] = str(gateway) if gateway else node.get("gateway")
                node["hex"] = raw_hex or node.get("hex")
                node["decode_note"] = decode_note
                if node.get("alert") and node["alert"].get("code") not in {"F_CNT_RESET"}:
                    node["alert"] = None
            elif fcnt is not None and node.get("fcnt") is not None and fcnt < int(node["fcnt"]):
                # Keep old/out-of-order packets in the event stream without rewinding live state.
                level = "warn"
        append_event_locked(eui, et, level, event_ts, payload, node, source)
        STATE["rev"] += 1
        trim_old_locked(received_ts)
        persist_locked()
        summary = f"[{source}] {name} {et}"
        if et == "up":
            summary += f" FCnt={fcnt if fcnt is not None else '-'} T={temp if temp is not None else '-'} H={hum if hum is not None else '-'} M={moist if moist is not None else '-'}"
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {summary}", flush=True)
    return True, "accepted"


def public_node_locked(node: dict, now: float) -> dict:
    eui = node["eui"]
    points = STATE["series"].get(eui, [])
    t24, t1 = now - 86400, now - 3600
    gaps = node.get("_gaps", [])
    out = {k: v for k, v in node.items() if not k.startswith("_")}
    out["configuration"] = deepcopy(NODE_CONFIGS.get(eui))
    out["n_24h"] = sum(1 for p in points if p[0] >= t24)
    out["n_1h"] = sum(1 for p in points if p[0] >= t1)
    out["missed_24h"] = sum(int(g.get("count", 0)) for g in gaps if num(g.get("ts")) is not None and g["ts"] >= t24)
    out["missed_1h"] = sum(int(g.get("count", 0)) for g in gaps if num(g.get("ts")) is not None and g["ts"] >= t1)
    out["missed"] = int(node.get("missed", 0))
    out["resets"] = int(node.get("resets", 0))
    out["resyncs"] = int(node.get("resyncs", 0))
    return out


def snapshot_locked() -> dict:
    now = utc_now()
    nodes = [public_node_locked(n, now) for n in STATE["nodes"].values()]
    # Known nodes remain at the top, discovered devices follow by name.
    order = {eui: i for i, eui in enumerate(KNOWN_NODES)}
    nodes.sort(key=lambda n: (order.get(n["eui"], 1000), n.get("name", "").lower()))
    mqtt = dict(STATE["mqtt"])
    mqtt["topics"] = list(MQTT_TOPICS)
    return {
        "version": VERSION, "epoch": STATE["epoch"], "rev": STATE["rev"],
        "started": STATE["started"], "now": now, "nodes": nodes,
        "events": STATE["events"][:], "mqtt": mqtt,
        "last_http": STATE["last_http"], "last_good": STATE["last_good"], "last_bad": STATE["last_bad"],
        "counts": dict(STATE["counts"]), "save": dict(STATE["save"]),
        "config": dict(STATE["config"]),
        "catalogVersion": str(PART_CATALOG.get("catalogVersion") or "missing"),
        "configuredNodeCount": len(NODE_CONFIGS),
    }


def clear_history() -> None:
    with LOCK:
        old_mqtt = STATE["mqtt"]
        old_started = STATE["started"]
        STATE.clear()
        reset = fresh_state()
        STATE.update(reset)
        # Configuration is stored separately from history; keep every configured node visible after a history clear.
        for configured_eui, cfg in NODE_CONFIGS.items():
            if configured_eui not in STATE["nodes"]:
                STATE["nodes"][configured_eui] = default_node(configured_eui, cfg.get("name") or "Configured node", len(STATE["nodes"]))
                STATE["series"].setdefault(configured_eui, [])
                STATE["raw"].setdefault(configured_eui, {})
            STATE["nodes"][configured_eui]["name"] = cfg.get("name") or STATE["nodes"][configured_eui].get("name")
        STATE["mqtt"] = old_mqtt
        STATE["started"] = old_started
        STATE["epoch"] = str(uuid.uuid4())
        STATE["rev"] += 1
        persist_locked(force=True)


def mqtt_status(state: str, error: str = "") -> None:
    with LOCK:
        m = STATE["mqtt"]
        if m.get("state") != state or m.get("error") != error:
            m["state"] = state
            m["error"] = error[:250]
            m["since"] = utc_now()
            STATE["rev"] += 1


def mqtt_listener() -> None:
    if not MQTT_ENABLED:
        mqtt_status("off", "")
        return
    if not MQTT_TOPICS:
        mqtt_status("error", "DASH_MQTT_TOPIC is empty")
        return
    backoff = 1
    while not STOP.is_set():
        client_bin = shutil.which("mosquitto_sub")
        if not client_bin:
            mqtt_status("error", "mosquitto_sub is not installed; HTTP webhook ingest still works")
            STOP.wait(15)
            continue
        cmd = [client_bin, "-h", MQTT_HOST, "-p", str(MQTT_PORT), "-q", "1", "-v", "-d"]
        if MQTT_USER:
            cmd.extend(["-u", MQTT_USER])
            if MQTT_PASSWORD:
                cmd.extend(["-P", MQTT_PASSWORD])
        for t in MQTT_TOPICS:
            cmd.extend(["-t", t])
        proc = None
        stderr_tail = deque(maxlen=10)
        try:
            mqtt_status("connecting", "")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1, universal_newlines=True)
            def read_stderr(stream, generation_proc):
                try:
                    for line in stream:
                        line = line.strip()
                        if not line:
                            continue
                        stderr_tail.append(line)
                        lo = line.lower()
                        if "connack (0)" in lo or "connected" in lo and "client" in lo:
                            mqtt_status("connected", "")
                        elif any(x in lo for x in ("connection refused", "not authorised", "not authorized", "error:", "name or service not known")):
                            mqtt_status("error", line)
                except Exception:
                    pass
            stderr_thread = threading.Thread(target=read_stderr, args=(proc.stderr, proc), daemon=True)
            stderr_thread.start()
            backoff = 1
            assert proc.stdout is not None
            for line in proc.stdout:
                if STOP.is_set():
                    break
                line = line.rstrip("\r\n")
                if not line:
                    continue
                # mosquitto_sub -v emits: topic + one space + payload.
                if " " in line:
                    topic, body = line.split(" ", 1)
                else:
                    topic, body = "", line
                topic_eui, topic_type = get_topic_bits(topic)
                with LOCK:
                    STATE["mqtt"]["last_topic"] = topic or STATE["mqtt"].get("last_topic", "")
                    STATE["mqtt"]["last_ts"] = utc_now()
                    STATE["mqtt"]["state"] = "connected"
                    STATE["mqtt"]["error"] = ""
                ingest_message(body, topic_type, "MQTT", topic)
            if not STOP.is_set():
                code = proc.poll()
                err = "; ".join(list(stderr_tail)[-3:]) or f"mosquitto_sub exited (code {code})"
                mqtt_status("error", err)
        except FileNotFoundError:
            mqtt_status("error", "mosquitto_sub is not installed; HTTP webhook ingest still works")
        except Exception as exc:
            mqtt_status("error", str(exc))
            print(f"[mqtt] Listener error: {exc}", flush=True)
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        if STOP.wait(backoff):
            break
        backoff = min(30, backoff * 2)


def save_loop() -> None:
    while not STOP.wait(1):
        with LOCK:
            persist_locked(force=False)


class DashboardHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def auth_token_for(username: str, expires: int, nonce: str) -> str:
    payload = f"{username}|{expires}|{nonce}".encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    sig = hmac.new(AUTH_SECRET, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return encoded + "." + sig


def auth_cookie_valid(cookie_header: str | None) -> bool:
    if not AUTH_PASSWORD:
        return True
    if not cookie_header:
        return False
    cookies = {}
    for item in cookie_header.split(";"):
        if "=" in item:
            key, value = item.strip().split("=", 1)
            cookies[key] = value
    token = cookies.get("edge_monitor_session", "")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with AUTH_SESSION_LOCK:
        now = time.time()
        for revoked_token, revoked_expiry in list(AUTH_REVOKED.items()):
            if revoked_expiry <= now:
                AUTH_REVOKED.pop(revoked_token, None)
        if token_hash in AUTH_REVOKED:
            return False
    if "." not in token:
        return False
    encoded, supplied_sig = token.rsplit(".", 1)
    expected = hmac.new(AUTH_SECRET, encoded.encode("ascii", "ignore"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied_sig, expected):
        return False
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        username, expiry, _nonce = payload.split("|", 2)
        return username == AUTH_USERNAME and int(expiry) > int(time.time())
    except (ValueError, UnicodeDecodeError):
        return False



def generate_chirpstack_codec(codec_fields: list[dict] | None) -> str:
    """Return the shared ChirpStack codec with this node's saved field names embedded."""
    if not CODEC_JS_FILE.exists():
        raise ValueError("chirpstack_codec.js is missing; install the codec files as a matched set")
    source = CODEC_JS_FILE.read_text(encoding="utf-8")
    marker = re.compile(r"const FIELD_KEYS = \{[\s\S]*?\n\};", re.MULTILINE)
    common = {str(field_id): str(item["key"]) for field_id, item in ID_DEFAULTS.items()}
    for field in codec_fields or []:
        if not isinstance(field, dict):
            continue
        try:
            field_id = int(field.get("id"))
        except (TypeError, ValueError):
            continue
        key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(field.get("key") or "").strip())[:64]
        if not 1 <= field_id <= 0xFE or not key:
            continue
        common[str(field_id)] = key
    replacement = "const FIELD_KEYS = " + json.dumps(common, ensure_ascii=False, indent=2) + ";"
    rendered, count = marker.subn(replacement, source, count=1)
    if count != 1:
        raise ValueError("Could not locate FIELD_KEYS in chirpstack_codec.js; refusing to create mismatched codec")
    return rendered

def field_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "node")).strip("-_")[:60]
    return slug or "node"


def field_sketch_name(value: str) -> str:
    """Return an Arduino sketch-folder/file basename using conservative characters."""
    name = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "EdgeNode")).strip("_")[:48] or "EdgeNode"
    return f"Node_{name}" if name[0].isdigit() else name


def extract_marked_source_block(source: str, start_marker: str, end_marker: str) -> str:
    """Extract exactly one marker-delimited source block, preserving bytes/line endings in the text."""
    lines = source.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if start_marker in line]
    ends = [i for i, line in enumerate(lines) if end_marker in line]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError(f"Expected exactly one ordered source block: {start_marker} ... {end_marker}")
    return "".join(lines[starts[0]:ends[0] + 1])


class Handler(BaseHTTPRequestHandler):
    server_version = "EdgeMonitor/" + VERSION

    def log_message(self, _fmt, *_args):
        return

    def send_bytes(self, status: int, content_type: str, body: bytes, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, status: int, data, headers: dict | None = None):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_bytes(status, "application/json; charset=utf-8", body, headers)

    def auth_ok(self) -> bool:
        if not AUTH_PASSWORD:
            return True
        return auth_cookie_valid(self.headers.get("Cookie"))

    def auth_required_response(self) -> None:
        self.send_json(401, {"ok": False, "error": "Please sign in again."})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/auth":
            self.send_json(200, {"required": bool(AUTH_PASSWORD), "authenticated": self.auth_ok(),
                                 "username": AUTH_USERNAME if self.auth_ok() else None})
            return
        if (path.startswith("/api/") and path not in ("/api/auth",) or path == "/clear") and not self.auth_ok():
            self.auth_required_response()
            return
        if path == "/api/state":
            gw_radio = get_cached_gateway_radio()
            with LOCK:
                data = snapshot_locked()
            data["gateway_radio"] = gw_radio
            self.send_json(200, data)
            return
        if path == "/api/catalog":
            global PART_CATALOG
            PART_CATALOG = load_catalog_document()
            self.send_json(200, PART_CATALOG)
            return
        if path == "/api/payload-registry":
            self.send_json(200, EMF1_REGISTRY)
            return
        if path == "/api/build-status":
            registry_hash = hashlib.sha256(EMF1_REGISTRY_FILE.read_bytes()).hexdigest() if EMF1_REGISTRY_FILE.exists() else None
            self.send_json(200, {
                "ok": True, "buildServiceConfigured": False, "otaCoreRequired": True,
                "otaCoreVersion": "edge-ota-core-v1", "otaCoreContractSha256": json.loads(OTA_CORE_CONTRACT_FILE.read_text(encoding="utf-8")).get("blockSha256") if OTA_CORE_CONTRACT_FILE.exists() else None, "sourceAssembly": "single-sketch-ota-prefix-plus-emf1-application", "firmwareSourceArtifact": "one-assembled-ino", "payloadCodecStandard": "Edge Measurement Format v1 (EMF1)",
                "payloadCodecMagic": "0xE1", "payloadCodecFPort": EMF1_FPORT,
                "codeGenerationFormats": ["edge_measurement_v1"],
                "legacyReceiveCompatibility": bool(EMF1_REGISTRY.get("legacyReceiveCompatibility", {}).get("enabled")),
                "legacyGenerationEnabled": False, "codecRegistrySchema": EMF1_REGISTRY.get("schema"),
                "codecRegistrySha256": registry_hash, "compiler": None, "automaticOtaPush": False,
                "message": "The wizard assembles one Arduino .ino: fixed WLAN OTA maintenance prefix followed by generated EMF1/sensor application code. Field map/configuration/ChirpStack codecs are sidecar build inputs. Legacy Fixed6 is receive-only compatibility. Compilation and automatic WLAN installation remain future milestones."
            })
            return
        if path == "/api/configs":
            with LOCK:
                data = {"schemaVersion": CONFIG_SCHEMA_VERSION, "catalogVersion": PART_CATALOG.get("catalogVersion"), "configs": deepcopy(NODE_CONFIGS)}
            self.send_json(200, data)
            return
        if path == "/api/series":
            try:
                since = max(0.0, float(parse_qs(parsed.query).get("since", ["0"])[0]))
            except (ValueError, TypeError):
                since = 0.0
            with LOCK:
                data = {"epoch": STATE["epoch"], "series": {
                    eui: [p[:] for p in points if p[0] > since]
                    for eui, points in STATE["series"].items()
                }}
            self.send_json(200, data)
            return
        if path == "/api/raw":
            eui = str(parse_qs(parsed.query).get("eui", [""])[0]).lower()
            with LOCK:
                raw = STATE["raw"].get(eui, {})
                data = {"raw": raw}
            self.send_json(200, data)
            return
        if path == "/healthz":
            with LOCK:
                m = dict(STATE["mqtt"])
                data = {"ok": True, "version": VERSION, "mqtt": m.get("state"), "nodes": len(STATE["nodes"])}
            self.send_json(200, data)
            return
        if path == "/clear":
            # Retain legacy URL support, although the modern UI uses POST /api/clear.
            clear_history()
            self.send_bytes(303, "text/plain; charset=utf-8", b"History cleared.\n", {"Location": "/"})
            return
        if path in ("/", "/index.html"):
            try:
                body = HTML_FILE.read_bytes()
                self.send_bytes(200, "text/html; charset=utf-8", body)
            except Exception as exc:
                self.send_bytes(500, "text/plain; charset=utf-8", f"Dashboard HTML could not be read: {exc}\n".encode())
            return
        if path == "/favicon.ico":
            self.send_bytes(204, "image/x-icon", b"")
            return
        self.send_bytes(404, "text/plain; charset=utf-8", b"Not found\n")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/api/login", "/api/logout"):
            if parsed.path == "/api/logout":
                existing_cookie = self.headers.get("Cookie", "")
                old_token = ""
                for item in existing_cookie.split(";"):
                    if item.strip().startswith("edge_monitor_session="):
                        old_token = item.strip().split("=", 1)[1]
                        break
                if old_token and AUTH_PASSWORD:
                    try:
                        encoded = old_token.rsplit(".", 1)[0]
                        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
                        _username, expiry, _nonce = payload.split("|", 2)
                        with AUTH_SESSION_LOCK:
                            AUTH_REVOKED[hashlib.sha256(old_token.encode("utf-8")).hexdigest()] = float(expiry)
                    except Exception:
                        pass
                cookie = "edge_monitor_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
                if AUTH_COOKIE_SECURE:
                    cookie += "; Secure"
                self.send_json(200, {"ok": True}, {"Set-Cookie": cookie})
                return
            if not AUTH_PASSWORD:
                self.send_json(200, {"ok": True, "authenticated": True})
                return
            ip = self.client_address[0] if self.client_address else "unknown"
            now = time.time()
            with AUTH_FAIL_LOCK:
                recent = [t for t in AUTH_FAILS.get(ip, []) if now - t < 60]
                AUTH_FAILS[ip] = recent
                blocked = len(recent) >= 5
            if blocked:
                self.send_json(429, {"ok": False, "error": "Too many failed attempts. Wait one minute and retry."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 8192:
                    raise ValueError("Invalid sign-in request")
                auth_doc = json.loads(self.rfile.read(length).decode("utf-8"))
                username = str(auth_doc.get("username", ""))
                password = str(auth_doc.get("password", ""))
            except Exception:
                self.send_json(400, {"ok": False, "error": "Invalid sign-in request"})
                return
            if not (hmac.compare_digest(username.encode("utf-8"), AUTH_USERNAME.encode("utf-8")) and hmac.compare_digest(password.encode("utf-8"), AUTH_PASSWORD.encode("utf-8"))):
                with AUTH_FAIL_LOCK:
                    AUTH_FAILS.setdefault(ip, []).append(now)
                self.send_json(401, {"ok": False, "error": "Username or password was incorrect."})
                return
            with AUTH_FAIL_LOCK:
                AUTH_FAILS.pop(ip, None)
            token = auth_token_for(AUTH_USERNAME, int(now) + AUTH_SESSION_SECONDS, secrets.token_urlsafe(18))
            cookie = f"edge_monitor_session={token}; Path=/; Max-Age={AUTH_SESSION_SECONDS}; HttpOnly; SameSite=Strict"
            if AUTH_COOKIE_SECURE:
                cookie += "; Secure"
            self.send_json(200, {"ok": True}, {"Set-Cookie": cookie})
            return

        # ChirpStack HTTP webhook ingestion (POST /?event=up) deliberately stays public;
        # Compilation proxy also stays accessible without auth so nodes can be updated.
        if parsed.path.startswith("/api/") and parsed.path != "/api/compile" and not self.auth_ok():
            self.auth_required_response()
            return

        if parsed.path in ("/api/field-package", "/api/firmware-source"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 256 * 1024:
                self.send_json(413 if length > 256 * 1024 else 400, {"ok": False, "error": "Codegen request must be 1–262144 bytes"})
                return
            try:
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                cfg = validate_node_config(req.get("configuration", {}), PART_CATALOG)
                field_doc = req.get("fieldPackage", {})
                firmware_source = req.get("firmwareSource", "")
                if not isinstance(field_doc, dict):
                    raise ValueError("Field package metadata must be an object.")
                if field_doc.get("payloadCodec") != "edge_measurement_v1" or int(field_doc.get("codecFPort", -1)) != EMF1_FPORT:
                    raise ValueError("Codegen contract failed: generated field package must declare EMF1 v1 on FPort 10.")
                if field_doc.get("expectedNodeDevEui") != cfg.get("devEui"):
                    raise ValueError("Codegen contract failed: field package DevEUI does not match the validated configuration.")
                if field_doc.get("codecRegistrySchema") != EMF1_REGISTRY.get("schema"):
                    raise ValueError("Codegen contract failed: field package registry schema is missing or mismatched.")
                if field_doc.get("sourceAssembly") != "single-sketch-ota-prefix-plus-emf1-application":
                    raise ValueError("Codegen contract failed: firmware must be assembled as one .ino with the OTA prefix before generated EMF1 application code.")
                sketch_name = field_sketch_name(cfg.get("name", "EdgeNode"))
                expected_source_path = f"firmware/{sketch_name}/{sketch_name}.ino"
                if field_doc.get("firmwareSourceFile") != f"{sketch_name}/{sketch_name}.ino":
                    raise ValueError("Codegen contract failed: browser sketch filename does not match the normalized Arduino sketch name.")
                def stable_field_contract(items):
                    keys = ("id", "key", "sourceKey", "sensorId", "sourceOccurrence", "sensorIndex", "fieldIndex", "typeCode", "wireTypeName", "scale", "unit", "label", "partId", "sensorLabel")
                    output = []
                    for item in items if isinstance(items, list) else []:
                        output.append({k: item.get(k) for k in keys})
                    return output
                if stable_field_contract(field_doc.get("codecFields")) != stable_field_contract(cfg.get("codecFields")):
                    raise ValueError("Codegen contract failed: browser field map differs from server-normalized field map. Reopen the wizard, save the configuration, and regenerate the bundle.")
                if not isinstance(firmware_source, str) or len(firmware_source) > 200_000:
                    raise ValueError("Assembled firmware source is missing or too large")
                required_ota_markers = ("edge-ota-core-v1", "void otaBegin()", "void otaHandleClient()", "Update.begin", "FIXED WLAN OTA CORE START")
                missing_ota = [marker for marker in required_ota_markers if marker not in firmware_source]
                if missing_ota:
                    raise ValueError("OTA-first template contract failed; missing required core marker(s): " + ", ".join(missing_ota))
                required_codec_markers = ("EDGE_MEASUREMENT_FORMAT_V1", "EMF1_MAGIC", "0xE1", "EDGE_EMF_V1_FPORT", "emfAddValue", "emfCommonType", "emfTypeSpec")
                missing_codec = [marker for marker in required_codec_markers if marker not in firmware_source]
                if missing_codec:
                    raise ValueError("EMF1 payload codec contract failed; missing required marker(s): " + ", ".join(missing_codec))
                if "LEGACY_FIXED6" in firmware_source or "legacyPack(" in firmware_source:
                    raise ValueError("Codegen contract failed: Legacy Fixed6 must not be emitted by the generator.")
                if firmware_source.count("FIXED WLAN OTA CORE START") != 1 or firmware_source.count("FIXED WLAN OTA CORE END") != 1:
                    raise ValueError("OTA source must contain exactly one fixed maintenance block.")
                core_start = firmware_source.index("FIXED WLAN OTA CORE START")
                core_end = firmware_source.index("FIXED WLAN OTA CORE END")
                app_marker = firmware_source.find("// ===== GENERATED SENSOR INTEGRATIONS START =====")
                if not (core_start < core_end < app_marker):
                    raise ValueError("Source assembly invalid: generated EMF1/sensor code must follow the fixed OTA core prefix.")
                ota_block = extract_marked_source_block(firmware_source, "FIXED WLAN OTA CORE START", "FIXED WLAN OTA CORE END")
                prefix = firmware_source[:app_marker]
                source_bytes = firmware_source.encode("utf-8")
                source_hash = hashlib.sha256(source_bytes).hexdigest()
                ota_block_hash = hashlib.sha256(ota_block.encode("utf-8")).hexdigest()
                prefix_hash = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
                if not OTA_CORE_CONTRACT_FILE.exists():
                    raise ValueError("Protected OTA-core contract is missing; refusing to create source output.")
                ota_contract = json.loads(OTA_CORE_CONTRACT_FILE.read_text(encoding="utf-8"))
                if ota_contract.get("version") != "edge-ota-core-v1" or ota_contract.get("blockSha256") != ota_block_hash:
                    raise ValueError("Protected OTA core hash mismatch. Refusing to download source; an intentional core change requires a version/hash review.")
                registry_hash = hashlib.sha256(EMF1_REGISTRY_FILE.read_bytes()).hexdigest() if EMF1_REGISTRY_FILE.exists() else None
                source_path = f"firmware/{sketch_name}/{sketch_name}.ino"
                build_manifest = {
                    "manifestSchema": "edge-monitor-firmware-build-v1",
                    "artifactType": "assembled-arduino-source",
                    "sourceAssembly": "single-sketch-ota-prefix-plus-emf1-application",
                    "sourceFile": source_path,
                    "sourceSha256": source_hash,
                    "sourceSizeBytes": len(source_bytes),
                    "sourcePrefixSha256": prefix_hash,
                    "otaCore": {"version": "edge-ota-core-v1", "position": "prefix", "blockSha256": ota_block_hash,
                                "contractFile": "ota_core_contract.json", "startMarker": "FIXED WLAN OTA CORE START", "endMarker": "FIXED WLAN OTA CORE END"},
                    "payload": {"standard": "EMF1 v1", "fPort": EMF1_FPORT, "registrySchema": EMF1_REGISTRY.get("schema"), "registrySha256": registry_hash},
                    "node": {"name": cfg.get("name"), "devEui": cfg.get("devEui"), "firmwareProfile": cfg.get("firmwareProfile")},
                    "build": {"ready": False, "compiledBinaryIncluded": False, "status": "source-only-template-not-compiled",
                              "requiredBeforeCompile": ["select exact ESP32 board/FQBN and partition layout", "integrate the selected LoRaWAN library and uplink callback", "resolve sensor driver dependencies and pin conflicts", "validate region and maximum application payload size", "harden OTA authentication and firmware verification"]}
                }
                package_doc = {
                    "packageType": "edge-monitor-single-sketch-field-job", "schemaVersion": 6,
                    "catalogVersion": PART_CATALOG.get("catalogVersion", "unknown"),
                    "preparedAt": utc_now(), "expectedNodeDevEui": cfg["devEui"],
                    "configuration": cfg, "otaCoreVersion": "edge-ota-core-v1",
                    "sourceAssembly": "single-sketch-ota-prefix-plus-emf1-application", "firmwareSourceFile": source_path,
                    "firmwareSourceSha256": source_hash, "otaCoreBlockSha256": ota_block_hash,
                    "buildManifest": "BUILD_MANIFEST.json",
                    "payloadCodec": "edge_measurement_v1", "payloadCodecVersion": "EMF1",
                    "codecFPort": EMF1_FPORT, "codecRegistrySchema": EMF1_REGISTRY.get("schema"),
                    "codecRegistrySha256": registry_hash, "codecFields": cfg.get("codecFields", []),
                    "otaCoreRequired": True, "firmwareBinaryIncluded": False,
                    "configurationAppliedToDevice": False, "buildServiceConfigured": False,
                    "codeGenerationFormatCount": 1, "buildStatus": "single-assembled-source-only-not-compiled",
                    "notice": "The firmware application is one assembled Arduino .ino: the fixed edge-ota-core-v1 maintenance prefix followed by generated EMF1 codec and sensor integrations. Configuration, build manifest and ChirpStack codecs are sidecar inputs to the future compiler. This is source-only, not compiled firmware; LoRaWAN uplink integration, board/dependency resolution, compiler service and automatic WLAN installation are not yet configured. Legacy Fixed6 is receive-only and is not generated."
                }
                memory = io.BytesIO()
                with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                    bundle.writestr("field-job.json", json.dumps(package_doc, indent=2, ensure_ascii=False))
                    bundle.writestr("node-config.json", json.dumps(cfg, indent=2, ensure_ascii=False))
                    bundle.writestr("BUILD_MANIFEST.json", json.dumps(build_manifest, indent=2, ensure_ascii=False))
                    bundle.writestr(source_path, firmware_source)
                    if CODEC_JS_FILE.exists():
                        bundle.write(CODEC_JS_FILE, "chirpstack_codec_universal.js")
                        bundle.writestr("chirpstack_codec.js", generate_chirpstack_codec(cfg.get("codecFields", [])))
                    if CODEC_STANDARD_FILE.exists():
                        bundle.write(CODEC_STANDARD_FILE, "PAYLOAD_CODEC_STANDARD.md")
                    if EMF1_REGISTRY_FILE.exists():
                        bundle.write(EMF1_REGISTRY_FILE, "emf1_registry.json")
                    if CODEGEN_SCRIPT_FILE.exists():
                        bundle.write(CODEGEN_SCRIPT_FILE, "build_emf1_artifacts.py")
                    if OTA_CORE_CONTRACT_FILE.exists():
                        bundle.write(OTA_CORE_CONTRACT_FILE, "ota_core_contract.json")
                    bundle.writestr("README.txt", "Edge Monitor EMF1 single-sketch field job\n\nThe firmware source exists as exactly one Arduino sketch: " + source_path + ". Its prefix contains the fixed WLAN OTA maintenance core (edge-ota-core-v1); the generated EMF1 encoder and sensor integrations follow it in the same .ino. There is no separate OTA source file to concatenate.\n\nBUILD_MANIFEST.json records source SHA-256, OTA block SHA-256, prefix SHA-256, EMF1 FPort and registry fingerprint. Configuration, build manifest and ChirpStack codec files are sidecar inputs for the future compiler, not chunks of firmware source.\n\nInstall chirpstack_codec.js (the configuration-bound field map) in the matching ChirpStack device profile. chirpstack_codec_universal.js is the generic fallback and names unknown custom IDs as field_XX. Legacy Fixed6 is receive-only compatibility for existing installed nodes; do not generate new firmware using it.\n\nSOURCE ONLY: no compiled .bin is included. The source still needs an exact ESP32 board/partition target, LoRaWAN uplink integration, library/dependency resolution, sensor/region/payload validation, and production OTA security review. The compiler server and automatic WLAN push are not configured. Do not flash this file as production firmware.\n")
                if parsed.path == "/api/firmware-source":
                    with zipfile.ZipFile(io.BytesIO(memory.getvalue()), "r") as checked_bundle:
                        assembled_source = checked_bundle.read(source_path)
                    self.send_bytes(200, "text/plain; charset=utf-8", assembled_source,
                                    {"Content-Disposition": f'attachment; filename="{sketch_name}.ino"'})
                else:
                    name = field_slug(cfg.get("name", "node"))
                    self.send_bytes(200, "application/zip", memory.getvalue(), {"Content-Disposition": f'attachment; filename="field-job-{name}.zip"'})
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_json(400, {"ok": False, "error": "Invalid JSON"})
            except ValueError as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                print(f"[package] Build failed: {exc}", flush=True)
                self.send_json(500, {"ok": False, "error": "Could not prepare field package"})
            return

        if parsed.path == "/api/configs":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 256 * 1024:
                self.send_json(413 if length > 256 * 1024 else 400, {"ok": False, "error": "Configuration request must be 1–262144 bytes"})
                return
            try:
                doc = json.loads(self.rfile.read(length).decode("utf-8", errors="strict"))
                cfg = validate_node_config(doc, PART_CATALOG)
                with LOCK:
                    eui = cfg["devEui"]
                    old_cfg = NODE_CONFIGS.get(eui)
                    NODE_CONFIGS[eui] = cfg
                    try:
                        save_config_store_locked()
                    except Exception:
                        if old_cfg is None:
                            NODE_CONFIGS.pop(eui, None)
                        else:
                            NODE_CONFIGS[eui] = old_cfg
                        raise
                    if eui not in STATE["nodes"]:
                        STATE["nodes"][eui] = default_node(eui, cfg["name"], len(STATE["nodes"]))
                        STATE["series"].setdefault(eui, [])
                        STATE["raw"].setdefault(eui, {})
                    STATE["nodes"][eui]["name"] = cfg["name"]
                    STATE["nodes"][eui]["interval"] = cfg["reportingIntervalSeconds"]
                    STATE["rev"] += 1
                    persist_locked(force=True)
                cs_sync = chirpstack_sync_device(cfg)
                self.send_json(200, {"ok": True, "config": cfg, "chirpstack": cs_sync})
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_json(400, {"ok": False, "error": "Invalid JSON"})
            except ValueError as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                print(f"[config] Save failed: {exc}", flush=True)
                self.send_json(500, {"ok": False, "error": "Could not save configuration; check dashboard logs"})
            return

        if parsed.path == "/api/nodes/reset-join":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 16384:
                    self.send_json(400, {"ok": False, "error": "Invalid request size"})
                    return
                doc = json.loads(self.rfile.read(length).decode("utf-8", errors="strict"))
                eui = str(doc.get("devEui") or doc.get("eui") or "").lower().replace(":", "").replace("-", "").strip()
                res = chirpstack_reset_join(eui)
                self.send_json(200 if res.get("ok") else 400, res)
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/api/nodes/delete":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 16384:
                    self.send_json(400, {"ok": False, "error": "Invalid request size"})
                    return
                doc = json.loads(self.rfile.read(length).decode("utf-8", errors="strict"))
                eui = str(doc.get("devEui") or doc.get("eui") or "").lower().replace(":", "").replace("-", "").strip()
                if not re.fullmatch(r"[0-9a-f]{16}", eui):
                    self.send_json(400, {"ok": False, "error": "DevEUI must be 16 hexadecimal characters"})
                    return
                cs_del = chirpstack_delete_device(eui)
                with LOCK:
                    NODE_CONFIGS.pop(eui, None)
                    try:
                        save_config_store_locked()
                    except Exception:
                        pass
                    STATE["nodes"].pop(eui, None)
                    STATE["series"].pop(eui, None)
                    STATE["raw"].pop(eui, None)
                    STATE["rev"] += 1
                    persist_locked(force=True)
                self.send_json(200, {"ok": True, "devEui": eui, "chirpstack": cs_del})
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
            return
        if parsed.path == "/api/clear":
            clear_history()
            self.send_json(200, {"ok": True, "epoch": STATE["epoch"]})
            return

        if parsed.path == "/api/compile":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 5 * 1024 * 1024:
                    self.send_json(400, {"ok": False, "error": "Invalid compilation request size"})
                    return
                body = self.rfile.read(length)
                req = urllib.request.Request(
                    COMPILER_BRIDGE_URL,
                    data=body,
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = resp.read()
                    self.send_response(resp.status)
                    for k, v in resp.getheaders():
                        if k.lower() in ("content-type", "content-disposition", "content-length"):
                            self.send_header(k, v)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
            except urllib.error.HTTPError as exc:
                try:
                    err_json = json.loads(exc.read().decode("utf-8"))
                    self.send_json(exc.code, err_json)
                except Exception:
                    self.send_json(exc.code, {"ok": False, "error": f"Compiler error: HTTP {exc.code}"})
            except Exception as exc:
                self.send_json(502, {"ok": False, "error": f"Compiler bridge unreachable at {COMPILER_BRIDGE_URL}: {exc}"})
            return

        if parsed.path.startswith("/api/"):
            self.send_json(404, {"ok": False, "error": f"API endpoint {parsed.path} not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 0 or length > MAX_BODY:
            self.send_bytes(413, "text/plain; charset=utf-8", b"Request body too large\n")
            return
        try:
            raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
        except Exception:
            self.send_bytes(400, "text/plain; charset=utf-8", b"Could not read request body\n")
            return
        event_param = parse_qs(parsed.query).get("event", [None])[0]
        accepted, reason = ingest_message(raw, event_param, "HTTP", "")
        # Return 200 for valid-but-unknown events so ChirpStack does not retry forever.
        # Invalid JSON gets 400, which is visible in ChirpStack's HTTP integration logs.
        if reason == "invalid JSON":
            self.send_bytes(400, "text/plain; charset=utf-8", b"Invalid JSON\n")
        else:
            self.send_bytes(200, "text/plain; charset=utf-8", ("OK" if accepted else "IGNORED: " + reason).encode("utf-8"))


def main() -> None:
    print(f"Edge Monitor {VERSION} starting; HTTP endpoint http://{HOST}:{PORT}/", flush=True)
    print(f"History: {HISTORY_FILE}; HTML: {HTML_FILE}", flush=True)
    print(f"Node configs: {CONFIGS_FILE}; catalogue: {CATALOG_FILE}; configured_nodes={len(NODE_CONFIGS)}", flush=True)
    print(f"MQTT: {'enabled' if MQTT_ENABLED else 'disabled'} at {MQTT_HOST}:{MQTT_PORT}; topics={MQTT_TOPICS}", flush=True)
    print(f"Technician login: {'enabled' if AUTH_PASSWORD else 'disabled (set DASH_AUTH_PASSWORD to enable)'}", flush=True)
    threading.Thread(target=mqtt_listener, name="mqtt-listener", daemon=True).start()
    threading.Thread(target=save_loop, name="history-save", daemon=True).start()
    server = DashboardHTTPServer((HOST, PORT), Handler)

    def shutdown(_sig=None, _frame=None):
        if STOP.is_set():
            return
        STOP.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        STOP.set()
        with LOCK:
            persist_locked(force=True)
        server.server_close()
        print("Edge Monitor stopped.", flush=True)


if __name__ == "__main__":
    main()
