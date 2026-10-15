#!/usr/bin/env python3
"""Edge Monitor: ChirpStack v4 MQTT + HTTP uplink dashboard backend.

Standard-library-only backend for the accompanying index.html. MQTT ingest uses
mosquitto_sub so the service can run on small gateway computers without pip deps.
"""
from __future__ import annotations

import base64
import bisect
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "2.1.0"
HOST = os.environ.get("DASH_HOST", "0.0.0.0")
PORT = int(os.environ.get("DASH_PORT", "8026"))
HISTORY_FILE = Path(os.environ.get("DASH_HISTORY_FILE", "/root/edge_monitor_state.json"))
HTML_FILE = Path(os.environ.get("DASH_HTML_FILE", "/root/index.html"))
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

KNOWN_NODES = {
    "d251f42dc4918f96": "ENode1",
    "b1e299ff3eed5d1d": "Node 2",
}

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
        "temperature": None, "humidity": None, "moisture": None,
        "rssi": None, "snr": None, "sf": None, "dr": None,
        "bw": None, "cr": None, "freq": None, "channel": None,
        "adr": None, "fcnt": None, "f_port": None, "dev_addr": None,
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


STATE = load_state()


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


def decode_sensor_bytes(raw: bytes) -> tuple[float | None, float | None, int | None]:
    if len(raw) < 6:
        return None, None, None
    temp_raw = int.from_bytes(raw[0:2], "big", signed=True)
    hum_raw = int.from_bytes(raw[2:4], "big", signed=False)
    moist_raw = int.from_bytes(raw[4:6], "big", signed=False)
    temperature = None if temp_raw == -32768 else temp_raw / 100.0
    humidity = None if hum_raw == 65535 else hum_raw / 100.0
    moisture = moist_raw
    return temperature, humidity, moisture


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
    byte_t, byte_h, byte_m = decode_sensor_bytes(raw_bytes) if et == "up" else (None, None, None)

    # The included patched firmware uses -32768/65535 as invalid-DHT markers.
    # These must override a codec's numeric conversion of the sentinel bytes.
    sentinel_temp = len(raw_bytes) >= 2 and int.from_bytes(raw_bytes[0:2], "big", signed=True) == -32768
    sentinel_hum = len(raw_bytes) >= 4 and int.from_bytes(raw_bytes[2:4], "big", signed=False) == 65535
    temp = None if sentinel_temp else (obj_t if obj_t is not None else byte_t)
    hum = None if sentinel_hum else (obj_h if obj_h is not None else byte_h)
    moist = obj_m if obj_m is not None else byte_m
    decode_note = None
    if decode_error:
        decode_note = decode_error
    elif et == "up" and raw_bytes and obj:
        mismatches = []
        if obj_t is not None and byte_t is not None and not sentinel_temp and abs(obj_t - byte_t) > 0.2:
            mismatches.append(f"codec temperature {obj_t:g} differs from raw-byte value {byte_t:g}")
        if obj_h is not None and byte_h is not None and not sentinel_hum and abs(obj_h - byte_h) > 0.5:
            mismatches.append(f"codec humidity {obj_h:g} differs from raw-byte value {byte_h:g}")
        if obj_m is not None and byte_m is not None and abs(obj_m - byte_m) > 1:
            mismatches.append(f"codec moisture {obj_m:g} differs from raw-byte value {byte_m:g}")
        if mismatches:
            decode_note = "; ".join(mismatches)

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
            values = [event_ts, temp, hum, moist, rssi, snr, fcnt]
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
                node["name"] = name or node.get("name")
                node["dev_addr"] = payload.get("devAddr") or payload.get("dev_addr") or node.get("dev_addr")
                node["fcnt"] = fcnt
                node["f_port"] = int_or_none(payload.get("fPort", payload.get("f_port")))
                node["dr"] = int_or_none(payload.get("dr"))
                node["adr"] = payload.get("adr") if isinstance(payload.get("adr"), bool) else None
                node["temperature"] = temp
                node["humidity"] = hum
                node["moisture"] = moist
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
    }


def clear_history() -> None:
    with LOCK:
        old_mqtt = STATE["mqtt"]
        old_started = STATE["started"]
        STATE.clear()
        reset = fresh_state()
        STATE.update(reset)
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

    def send_json(self, status: int, data):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_bytes(status, "application/json; charset=utf-8", body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/state":
            with LOCK:
                data = snapshot_locked()
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

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/clear":
            clear_history()
            self.send_json(200, {"ok": True, "epoch": STATE["epoch"]})
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
    print(f"MQTT: {'enabled' if MQTT_ENABLED else 'disabled'} at {MQTT_HOST}:{MQTT_PORT}; topics={MQTT_TOPICS}", flush=True)
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
