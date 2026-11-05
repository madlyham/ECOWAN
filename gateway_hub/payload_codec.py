#!/usr/bin/env python3
"""Edge Measurement Format v1 (EMF1) codec and legacy fixed-six adapter.

EMF1 wire format: 0xE1 header followed by records: field_id, type_code, value bytes.
Records use big-endian integer values. Type codes specify both storage width and scale.
"""
from __future__ import annotations

EMF1_MAGIC = 0xE1
EMF1_FPORT = 10
LEGACY_FIXED6_FPORT = 1
LEGACY_FIXED6_LENGTH = 6

EGDC_MAGIC = 0xE2
EGDC_FPORT = 11
EGDC_GRID_W = 32
EGDC_GRID_H = 24
EGDC_MAX_CELLS = 768

CUTOFF_PROFILES = {
    "conservative": 4,
    "balanced": 8,
    "extended": 10,
    "experimental": 16,
}

AS923_DR_MAP = {
    0: {"sf": 12, "bw": 125, "max_dwell0": 51, "max_dwell1": 0},
    1: {"sf": 11, "bw": 125, "max_dwell0": 51, "max_dwell1": 0},
    2: {"sf": 10, "bw": 125, "max_dwell0": 51, "max_dwell1": 11},
    3: {"sf": 9, "bw": 125, "max_dwell0": 115, "max_dwell1": 53},
    4: {"sf": 8, "bw": 125, "max_dwell0": 222, "max_dwell1": 125},
    5: {"sf": 7, "bw": 125, "max_dwell0": 222, "max_dwell1": 222},
    6: {"sf": 7, "bw": 250, "max_dwell0": 222, "max_dwell1": 222},
}

# code: name, byte width, signed, scale, semantic constraint
WIRE_TYPES = {
    0x01: ("bool", 1, False, 1.0, "bool"),
    0x02: ("u8", 1, False, 1.0, None),
    0x03: ("i8", 1, True, 1.0, None),
    0x11: ("u16", 2, False, 1.0, None),
    0x12: ("i16", 2, True, 1.0, None),
    0x21: ("u16_x0_1", 2, False, 0.1, None),
    0x22: ("i16_x0_1", 2, True, 0.1, None),
    0x23: ("u16_x0_01", 2, False, 0.01, None),
    0x24: ("i16_x0_01", 2, True, 0.01, None),
    0x31: ("u32", 4, False, 1.0, None),
    0x32: ("i32", 4, True, 1.0, None),
}

# Field IDs 0x01–0x7F are reserved for common measurement meanings.
# IDs 0x80–0xFE may be assigned by a saved node config; 0x00 and 0xFF are reserved.
FIELD_DEFINITIONS = {
    "temperature": {"id": 0x01, "typeCode": 0x24, "unit": "°C", "label": "Temperature"},
    "humidity": {"id": 0x02, "typeCode": 0x23, "unit": "%RH", "label": "Relative humidity"},
    "moisture": {"id": 0x03, "typeCode": 0x11, "unit": "ADC", "label": "Moisture raw ADC"},
    "moisture_raw_adc": {"id": 0x03, "typeCode": 0x11, "unit": "ADC", "label": "Moisture raw ADC"},
    "illuminance_lux": {"id": 0x04, "typeCode": 0x31, "unit": "lux", "label": "Illuminance"},
    "pressure_hpa": {"id": 0x05, "typeCode": 0x21, "unit": "hPa", "label": "Air pressure"},
    "co2_ppm": {"id": 0x06, "typeCode": 0x11, "unit": "ppm", "label": "CO₂"},
    "ph": {"id": 0x07, "typeCode": 0x24, "unit": "pH", "label": "pH"},
    "dissolved_oxygen_mg_l": {"id": 0x08, "typeCode": 0x23, "unit": "mg/L", "label": "Dissolved oxygen"},
    "conductivity_ms_cm": {"id": 0x09, "typeCode": 0x23, "unit": "mS/cm", "label": "Conductivity"},
    "flow_m3_h": {"id": 0x0A, "typeCode": 0x21, "unit": "m³/h", "label": "Flow rate"},
    "ammonia_mg_l": {"id": 0x0B, "typeCode": 0x23, "unit": "mg/L", "label": "Ammonia"},
    "cod_mg_l": {"id": 0x0C, "typeCode": 0x11, "unit": "mg/L", "label": "Chemical oxygen demand"},
    "digital_input": {"id": 0x0D, "typeCode": 0x01, "unit": "state", "label": "Digital input"},
    "battery_mv": {"id": 0x0E, "typeCode": 0x11, "unit": "mV", "label": "Battery voltage"},
    "presence": {"id": 0x0F, "typeCode": 0x01, "unit": "state", "label": "Presence"},
    "core_uptime_s": {"id": 0x10, "typeCode": 0x31, "unit": "s", "label": "Core uptime"},
}

ID_DEFAULTS = {v["id"]: {"key": k, **v} for k, v in FIELD_DEFINITIONS.items() if k not in {"moisture_raw_adc"}}
# Prefer one canonical key when different aliases share one field ID.
ID_DEFAULTS[0x03] = {"key": "moisture", **FIELD_DEFINITIONS["moisture"]}

EMF1_REGISTRY = {
    "schema": "edge-monitor-emf1-registry-v1",
    "magic": "0xE1",
    "fPort": EMF1_FPORT,
    "wireTypes": {f"0x{k:02X}": {"name": v[0], "bytes": v[1], "signed": v[2], "scale": v[3], "constraint": v[4]} for k, v in WIRE_TYPES.items()},
    "commonFields": FIELD_DEFINITIONS,
    "legacyReceiveCompatibility": {
        "enabled": True,
        "fPort": LEGACY_FIXED6_FPORT,
        "length": LEGACY_FIXED6_LENGTH,
    },
}


def _infer_type(key: str, unit: str) -> int:
    definition = FIELD_DEFINITIONS.get(key)
    if definition:
        return definition["typeCode"]
    k = key.lower()
    u = unit.lower().replace(" ", "")
    if any(t in k for t in ("digital", "presence", "door_open", "switch_state", "_state")) or u == "state":
        return 0x01
    if "raw" in k or "adc" in k or u == "adc":
        return 0x11
    if "temperature" in k or k.startswith("temp_") or u in {"°c", "c", "degc"}:
        return 0x24
    if "humidity" in k or u in {"%", "%rh", "%rel"}:
        return 0x23
    if "lux" in k or u == "lux":
        return 0x31
    if "pressure" in k and "hpa" in u or u == "hpa":
        return 0x21
    if "ph" == k or k.startswith("ph_") or u == "ph":
        return 0x24
    # Conservative default for a custom numeric field: signed 32-bit integer.
    # The generated integration must still define its engineering-unit conversion.
    return 0x32


def build_codec_fields(sensors: list | None, previous_fields: list[dict] | None = None) -> list[dict]:
    """Build stable field IDs and output keys for configured sensors.

    Existing fields are matched by stable sensor ID + source key + duplicate occurrence,
    not list position, so inserting a sensor or field does not silently rename old data.
    """
    rows = []
    for si, sensor in enumerate(sensors or []):
        if not isinstance(sensor, dict):
            continue
        sid = str(sensor.get("id") or f"sensor-{si + 1}")
        slabel = str(sensor.get("label") or sensor.get("partId") or f"Sensor {si + 1}")
        occurrence_counts: dict[str, int] = {}
        for fi, field in enumerate(sensor.get("fields") or []):
            if not isinstance(field, dict):
                continue
            source_key = str(field.get("key") or "").strip()
            if not source_key:
                continue
            occurrence = occurrence_counts.get(source_key, 0)
            occurrence_counts[source_key] = occurrence + 1
            rows.append({
                "sensorId": sid, "sensorLabel": slabel,
                "partId": str(sensor.get("partId") or "custom_module"),
                "fieldIndex": fi, "sourceOccurrence": occurrence,
                "sourceKey": source_key, "key": source_key,
                "label": str(field.get("label") or source_key),
                "unit": str(field.get("unit") or ""),
            })

    # Index prior mappings. Older saved mappings do not have sourceOccurrence; infer it
    # from field order within a sensor for a backwards-compatible one-time migration.
    old_groups: dict[str, list[dict]] = {}
    for old in previous_fields or []:
        if not isinstance(old, dict):
            continue
        sid = str(old.get("sensorId") or "")
        old_groups.setdefault(sid, []).append(old)
    previous_index: dict[tuple[str, str, int], dict] = {}
    for sid, group in old_groups.items():
        group.sort(key=lambda item: int(item.get("fieldIndex", 1_000_000)) if str(item.get("fieldIndex", "")).lstrip("-").isdigit() else 1_000_000)
        occurrence_counts: dict[str, int] = {}
        for old in group:
            source_key = str(old.get("sourceKey") or "")
            try:
                occurrence = int(old["sourceOccurrence"]) if old.get("sourceOccurrence") is not None else occurrence_counts.get(source_key, 0)
            except (TypeError, ValueError):
                occurrence = occurrence_counts.get(source_key, 0)
            occurrence_counts[source_key] = max(occurrence_counts.get(source_key, 0), occurrence + 1)
            if source_key:
                previous_index[(sid, source_key, occurrence)] = old

    reused: dict[int, dict] = {}
    reserved_ids: set[int] = set()
    for index, row in enumerate(rows):
        match_key = (row["sensorId"], row["sourceKey"], int(row["sourceOccurrence"]))
        old = previous_index.get(match_key)
        if old is None:
            continue
        try:
            old_id = int(old.get("id"))
            old_type = int(old.get("typeCode"))
        except (TypeError, ValueError):
            continue
        if not (1 <= old_id <= 0xFE) or old_id in reserved_ids or old_type not in WIRE_TYPES:
            continue
        reused[index] = old
        reserved_ids.add(old_id)

    # Preserve keys already in use by existing measurements before naming new fields.
    reserved_keys: set[str] = set()
    for index, old in reused.items():
        old_key = str(old.get("key") or rows[index]["sourceKey"])
        rows[index]["key"] = old_key
        reserved_keys.add(old_key)
    used_keys = set(reserved_keys)
    for index, row in enumerate(rows):
        if index in reused:
            continue
        base_key = row["sourceKey"]
        candidate = base_key
        suffix = 2
        while candidate in used_keys:
            candidate = f"{base_key}_{suffix}"
            suffix += 1
        row["key"] = candidate
        used_keys.add(candidate)

    used_ids: set[int] = set()
    next_custom = 0x80
    for index, row in enumerate(rows):
        source_key = row["sourceKey"]
        definition = FIELD_DEFINITIONS.get(source_key)
        default_type = definition["typeCode"] if definition else _infer_type(source_key, row["unit"])
        old = reused.get(index)
        if old is not None:
            field_id = int(old["id"])
            # Common fields retain their normative type; custom fields keep the prior wire type.
            type_code = default_type if definition else int(old["typeCode"])
            default_unit = definition["unit"] if definition else row["unit"]
            default_label = definition["label"] if definition else row["label"]
        else:
            wanted_id = definition["id"] if definition else None
            if wanted_id is not None and wanted_id not in used_ids and wanted_id not in reserved_ids:
                field_id = wanted_id
                type_code = default_type
                default_unit = definition["unit"]
                default_label = definition["label"]
            else:
                while next_custom in used_ids or next_custom in reserved_ids:
                    next_custom += 1
                if next_custom >= 0xFF:
                    raise ValueError("This node has too many unique measurement fields for EMF1")
                field_id = next_custom
                next_custom += 1
                type_code = default_type
                default_unit = row["unit"]
                default_label = row["label"]
        used_ids.add(field_id)
        spec = WIRE_TYPES[type_code]
        row.update({
            "id": field_id,
            "typeCode": type_code,
            "wireType": spec[0],
            "scale": spec[3],
            "label": row["label"] or default_label,
            "unit": row["unit"] or default_unit,
        })
    return rows


def decode_emf1(raw: bytes, codec_fields: list[dict] | None = None) -> tuple[dict, str | None]:
    if not raw:
        return {}, "Empty application payload"
    if raw[0] != EMF1_MAGIC:
        return {}, f"EMF1 header mismatch: expected E1, received {raw[0]:02X}"
    if len(raw) == 1:
        return {}, "EMF1 payload has header only; at least one measurement record is required"
    field_map = {int(item.get("id")): item for item in (codec_fields or []) if isinstance(item, dict) and item.get("id") is not None}
    values: dict[str, object] = {}
    pos = 1
    seen_ids: set[int] = set()
    while pos < len(raw):
        if len(raw) - pos < 2:
            return values, f"EMF1 truncated record header at byte {pos}"
        field_id, type_code = raw[pos], raw[pos + 1]
        pos += 2
        if field_id in (0x00, 0xFF):
            return values, f"EMF1 reserved field ID 0x{field_id:02X} is not allowed"
        if field_id in seen_ids:
            return values, f"EMF1 duplicate field ID 0x{field_id:02X} in one uplink"
        seen_ids.add(field_id)
        spec = WIRE_TYPES.get(type_code)
        if spec is None:
            return values, f"EMF1 unknown type code 0x{type_code:02X} at field 0x{field_id:02X}; cannot safely skip"
        canonical_field = ID_DEFAULTS.get(field_id)
        if canonical_field and int(canonical_field.get("typeCode")) != type_code:
            return values, f"EMF1 field 0x{field_id:02X} has type 0x{type_code:02X}; registry requires 0x{int(canonical_field.get('typeCode')):02X}"
        name, width, signed, scale, constraint = spec
        if len(raw) - pos < width:
            return values, f"EMF1 field 0x{field_id:02X} is truncated (needs {width} value bytes)"
        integer = int.from_bytes(raw[pos:pos + width], "big", signed=signed)
        pos += width
        if constraint == "bool" and integer not in (0, 1):
            return values, f"EMF1 bool field 0x{field_id:02X} must be 0 or 1"
        field = field_map.get(field_id) or ID_DEFAULTS.get(field_id)
        key = str(field.get("key") if field else f"field_{field_id:02X}")
        # The value type in the packet is authoritative; config provides meaning and display label.
        value = bool(integer) if constraint == "bool" else integer * scale
        values[key] = value
    return values, None


def decode_legacy_fixed6(raw: bytes) -> tuple[dict, str | None]:
    if len(raw) != LEGACY_FIXED6_LENGTH:
        return {}, f"Legacy Fixed6 requires exactly 6 bytes; received {len(raw)}"
    temp_raw = int.from_bytes(raw[0:2], "big", signed=True)
    hum_raw = int.from_bytes(raw[2:4], "big", signed=False)
    moisture_raw = int.from_bytes(raw[4:6], "big", signed=False)
    return {
        "temperature": None if temp_raw == -32768 else temp_raw / 100.0,
        "humidity": None if hum_raw == 65535 else hum_raw / 100.0,
        "moisture": moisture_raw,
    }, None


def decode_application_payload(raw: bytes, f_port: int | None, codec_fields: list[dict] | None = None) -> tuple[str | None, dict, str | None]:
    """Decode a node application payload without relying on the ChirpStack object codec."""
    if not raw:
        return None, {}, None
    if f_port == EGDC_FPORT or (f_port is None and len(raw) >= 6 and raw[0] == EGDC_MAGIC):
        values, error = decode_egdc_packet(raw)
        return "egdc_v2", values, error
    if raw[0] == EGDC_MAGIC:
        values, error = decode_egdc_packet(raw)
        return "egdc_v2", values, error
    # Explicit ports win. A missing fPort can still be decoded using the version byte/length.
    if f_port == LEGACY_FIXED6_FPORT:
        values, error = decode_legacy_fixed6(raw)
        return "legacy_fixed6_v1", values, error
    if f_port == EMF1_FPORT or (f_port is None and raw[0] == EMF1_MAGIC):
        values, error = decode_emf1(raw, codec_fields)
        return "edge_measurement_v1", values, error
    if f_port is None and len(raw) == LEGACY_FIXED6_LENGTH:
        values, error = decode_legacy_fixed6(raw)
        return "legacy_fixed6_v1", values, error
    if raw[0] == EMF1_MAGIC:
        values, error = decode_emf1(raw, codec_fields)
        return "edge_measurement_v1", values, error
    return None, {}, f"Unknown payload format (FPort={f_port}, length={len(raw)}, header=0x{raw[0]:02X})"


def crc8_egdc(data: bytes) -> int:
    """CRC-8 calculation with polynomial 0x07 (ATM/SMBus) over arbitrary bytes."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def encode_egdc_packet(
    keyframe_gen_id: int,
    frame_seq: int,
    pkt_idx: int,
    pkt_total: int,
    cells: list[dict],
    flags: int = 0,
) -> bytes:
    """Encode an EGDC v2 wire packet (6B header + priority cell records)."""
    body = bytearray()
    for c in cells:
        idx = int(c["cell_idx"])
        if idx < 0 or idx >= EGDC_MAX_CELLS:
            raise ValueError(f"cell_idx out of range: {idx}")
        mode = int(c.get("mode", 0))
        priority = int(c.get("priority", 0))
        cell_hdr = ((idx & 0x3FF) << 6) | ((mode & 0x03) << 4) | (priority & 0x0F)
        body.extend(cell_hdr.to_bytes(2, "big"))
        if mode == 0:
            rgb = int(c.get("rgb565", 0)) & 0xFFFF
            body.extend(rgb.to_bytes(2, "big"))
        elif mode == 1:
            luma = c.get("luma_4x4", [0] * 16)
            if len(luma) != 16:
                raise ValueError("Mode 1 requires 16 luma values")
            for i in range(8):
                hi = int(luma[2 * i]) & 0x0F
                lo = int(luma[2 * i + 1]) & 0x0F
                body.append((hi << 4) | lo)
        elif mode == 2:
            grad = c.get("gradient_8x8", [0] * 16)
            if len(grad) != 16:
                raise ValueError("Mode 2 requires 16 gradient values")
            body.extend(bytes([int(v) & 0xFF for v in grad]))
            chroma = int(c.get("chroma_anchor", 0)) & 0xFFFF
            body.extend(chroma.to_bytes(2, "big"))
        else:
            raise ValueError(f"Unknown mode {mode}")

    pkt_idx_total = ((pkt_idx & 0x0F) << 4) | ((max(1, pkt_total) - 1) & 0x0F)
    hdr_prefix = bytes([
        EGDC_MAGIC,
        keyframe_gen_id & 0xFF,
        frame_seq & 0xFF,
        pkt_idx_total,
        flags & 0xFF,
    ])
    crc = crc8_egdc(hdr_prefix + body)
    return hdr_prefix + bytes([crc]) + bytes(body)


def decode_egdc_packet(raw: bytes) -> tuple[dict, str | None]:
    """Decode an EGDC v2 wire packet into header fields and cell records."""
    if len(raw) < 6:
        return {}, f"Truncated EGDC v2 header ({len(raw)} bytes, expected at least 6)"
    if raw[0] != EGDC_MAGIC:
        return {}, f"Invalid EGDC magic byte: expected 0x{EGDC_MAGIC:02X}, received 0x{raw[0]:02X}"

    expected_crc = raw[5]
    computed_crc = crc8_egdc(raw[:5] + raw[6:])
    if expected_crc != computed_crc:
        return {}, f"EGDC CRC-8 mismatch: expected 0x{expected_crc:02X}, computed 0x{computed_crc:02X}"

    keyframe_gen_id = raw[1]
    frame_seq = raw[2]
    pkt_idx = (raw[3] >> 4) & 0x0F
    pkt_total = (raw[3] & 0x0F) + 1
    if pkt_idx >= pkt_total:
        return {}, f"Invalid packet index {pkt_idx} >= total {pkt_total}"

    flags = raw[4]
    cells = []
    pos = 6
    while pos < len(raw):
        if len(raw) - pos < 2:
            return {}, f"Truncated cell record header at byte {pos}"
        cell_hdr = int.from_bytes(raw[pos:pos + 2], "big")
        pos += 2
        cell_idx = (cell_hdr >> 6) & 0x3FF
        mode = (cell_hdr >> 4) & 0x03
        priority = cell_hdr & 0x0F
        if cell_idx >= EGDC_MAX_CELLS:
            return {}, f"Out-of-range cell_idx {cell_idx} >= {EGDC_MAX_CELLS}"

        if mode == 0:
            if len(raw) - pos < 2:
                return {}, f"Truncated Mode 0 cell payload at byte {pos}"
            rgb565 = int.from_bytes(raw[pos:pos + 2], "big")
            pos += 2
            cells.append({
                "cell_idx": cell_idx,
                "mode": 0,
                "priority": priority,
                "rgb565": rgb565,
            })
        elif mode == 1:
            if len(raw) - pos < 8:
                return {}, f"Truncated Mode 1 cell payload at byte {pos}"
            luma_4x4 = []
            for i in range(8):
                b = raw[pos + i]
                luma_4x4.append((b >> 4) & 0x0F)
                luma_4x4.append(b & 0x0F)
            pos += 8
            cells.append({
                "cell_idx": cell_idx,
                "mode": 1,
                "priority": priority,
                "luma_4x4": luma_4x4,
            })
        elif mode == 2:
            if len(raw) - pos < 18:
                return {}, f"Truncated Mode 2 cell payload at byte {pos}"
            gradient_8x8 = list(raw[pos:pos + 16])
            chroma_anchor = int.from_bytes(raw[pos + 16:pos + 18], "big")
            pos += 18
            cells.append({
                "cell_idx": cell_idx,
                "mode": 2,
                "priority": priority,
                "gradient_8x8": gradient_8x8,
                "chroma_anchor": chroma_anchor,
            })
        else:
            return {}, f"Unknown cell mode {mode} at byte {pos - 2}"

    return {
        "keyframe_gen_id": keyframe_gen_id,
        "frame_seq": frame_seq,
        "pkt_idx": pkt_idx,
        "pkt_total": pkt_total,
        "flags": flags,
        "cells": cells,
    }, None


def pack_egdc_frames(
    keyframe_gen_id: int,
    frame_seq: int,
    cells: list[dict],
    max_app_bytes: int = 222,
    flags: int = 0,
) -> list[bytes]:
    """Sort cells by priority descending and pack into minimum necessary EGDC packets."""
    sorted_cells = sorted(cells, key=lambda c: int(c.get("priority", 0)), reverse=True)

    def cell_len(c: dict) -> int:
        m = int(c.get("mode", 0))
        return 4 if m == 0 else (10 if m == 1 else 20)

    batches: list[list[dict]] = []
    curr_batch: list[dict] = []
    curr_len = 0
    max_payload = max(0, max_app_bytes - 6)

    for c in sorted_cells:
        sz = cell_len(c)
        if curr_batch and (curr_len + sz > max_payload):
            batches.append(curr_batch)
            curr_batch = [c]
            curr_len = sz
        else:
            curr_batch.append(c)
            curr_len += sz

    if curr_batch or not batches:
        batches.append(curr_batch)

    total = len(batches)
    packets = []
    for idx, b in enumerate(batches):
        pkt = encode_egdc_packet(
            keyframe_gen_id=keyframe_gen_id,
            frame_seq=frame_seq,
            pkt_idx=idx,
            pkt_total=total,
            cells=b,
            flags=flags,
        )
        packets.append(pkt)
    return packets


def calculate_lora_airtime(
    app_bytes: int,
    sf: int = 7,
    bw_khz: float = 125.0,
    mac_overhead_bytes: int = 13,
    cr: int = 1,
    preamble_symbols: int = 8,
    crc_enabled: bool = True,
    implicit_header: bool = False,
    low_dr_opt: bool | None = None,
) -> dict:
    """Exact Semtech SX126x/SX127x LoRa airtime calculator distinguishing App, MAC, and PHY bytes."""
    import math
    phy_bytes = app_bytes + mac_overhead_bytes
    bw_hz = bw_khz * 1000.0
    t_sym_s = (2 ** sf) / bw_hz
    t_sym_ms = t_sym_s * 1000.0

    if low_dr_opt is None:
        de = 1 if t_sym_ms > 16.0 else 0
    else:
        de = 1 if low_dr_opt else 0

    t_preamble_ms = (preamble_symbols + 4.25) * t_sym_ms

    crc_flag = 1 if crc_enabled else 0
    ih_flag = 1 if implicit_header else 0

    numerator = 8 * phy_bytes - 4 * sf + 28 + 16 * crc_flag - 20 * ih_flag
    denominator = 4 * (sf - 2 * de)

    term = math.ceil(numerator / denominator) if denominator > 0 else 0
    payload_sym_nb = 8 + max(term * (cr + 4), 0)
    t_payload_ms = payload_sym_nb * t_sym_ms
    total_airtime_ms = t_preamble_ms + t_payload_ms

    return {
        "app_bytes": app_bytes,
        "mac_bytes": mac_overhead_bytes,
        "phy_bytes": phy_bytes,
        "airtime_ms": total_airtime_ms,
        "dwell_400ms_ok": total_airtime_ms <= 400.0,
        "sf": sf,
        "bw_khz": bw_khz,
    }


def check_as923_payload_limit(
    app_bytes: int,
    dr: int = 5,
    dwell_time_limited: bool = True,
    mac_overhead_bytes: int = 13,
) -> dict:
    """Evaluate application payload size against AS923 regional and 400ms dwell-time constraints."""
    dr_info = AS923_DR_MAP.get(dr)
    if not dr_info:
        return {"valid": False, "reason": f"Unknown DR {dr}", "airtime_ms": 0.0, "dwell_400ms_ok": False}

    max_app = dr_info["max_dwell1"] if dwell_time_limited else dr_info["max_dwell0"]
    airtime_info = calculate_lora_airtime(
        app_bytes=app_bytes,
        sf=dr_info["sf"],
        bw_khz=dr_info["bw"],
        mac_overhead_bytes=mac_overhead_bytes,
    )
    dwell_ok = airtime_info["dwell_400ms_ok"]
    valid = (app_bytes <= max_app) and (dwell_ok if dwell_time_limited else True)
    return {
        "valid": valid,
        "app_bytes": app_bytes,
        "max_app_bytes": max_app,
        "dr": dr,
        "dwell_time_limited": dwell_time_limited,
        "dwell_400ms_ok": dwell_ok,
        "airtime_ms": airtime_info["airtime_ms"],
        "phy_bytes": airtime_info["phy_bytes"],
    }


def evaluate_cutoff_policy(
    packets_required: int,
    profile: str = "balanced",
    airtime_per_pkt_ms: float = 276.7,
    queue_wait_ms: float = 0.0,
    recovery_overhead_ms: float = 0.0,
    keyframe_cost_ms: float = 5000.0,
) -> dict:
    """Cost-based and ceiling-based adaptive cutoff policy (conservative=4, balanced=8, extended=10, experimental=16)."""
    max_packets = CUTOFF_PROFILES.get(profile, 8)
    exceeded_packet_cap = packets_required > max_packets
    estimated_delta_cost = (packets_required * airtime_per_pkt_ms) + queue_wait_ms + recovery_overhead_ms
    exceeded_cost_cap = estimated_delta_cost > keyframe_cost_ms
    action = "keyframe" if (exceeded_packet_cap or exceeded_cost_cap) else "lora_delta"
    return {
        "action": action,
        "profile": profile,
        "max_packets": max_packets,
        "packets_required": packets_required,
        "exceeded_packet_cap": exceeded_packet_cap,
        "exceeded_cost_cap": exceeded_cost_cap,
        "estimated_delta_cost_ms": estimated_delta_cost,
        "keyframe_cost_ms": keyframe_cost_ms,
    }


def encode_emf1(records: list[tuple[int, int, int]]) -> bytes:
    """Test/reference encoder for integer wire records (id, type_code, encoded_integer)."""
    output = bytearray([EMF1_MAGIC])
    seen: set[int] = set()
    for field_id, type_code, integer in records:
        if not 1 <= int(field_id) <= 0xFE or field_id in seen:
            raise ValueError("field IDs must be unique integers from 1 through 254")
        spec = WIRE_TYPES.get(int(type_code))
        if not spec:
            raise ValueError(f"unknown wire type 0x{int(type_code):02X}")
        name, width, signed, _scale, constraint = spec
        if constraint == "bool" and int(integer) not in (0, 1):
            raise ValueError("boolean value must be 0 or 1")
        output.append(int(field_id))
        output.append(int(type_code))
        output.extend(int(integer).to_bytes(width, "big", signed=signed))
        seen.add(int(field_id))
    return bytes(output)
