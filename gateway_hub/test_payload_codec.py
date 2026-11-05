import unittest
from payload_codec import (
    decode_application_payload, decode_emf1, decode_legacy_fixed6,
    encode_emf1, build_codec_fields, EMF1_REGISTRY,
)


class PayloadCodecTests(unittest.TestCase):
    def test_emf1_reference_vector(self):
        raw = bytes.fromhex('E1 01 24 0B 40 02 23 0C 80 03 11 00 00')
        fmt, values, error = decode_application_payload(raw, 10)
        self.assertEqual(fmt, 'edge_measurement_v1')
        self.assertIsNone(error)
        self.assertAlmostEqual(values['temperature'], 28.8)
        self.assertAlmostEqual(values['humidity'], 32.0)
        self.assertEqual(values['moisture'], 0)

    def test_legacy_fixed6_vector(self):
        # -5.25 C, 50.00% RH, 1234 ADC
        raw = bytes.fromhex('FD F3 13 88 04 D2')
        fmt, values, error = decode_application_payload(raw, 1)
        self.assertEqual(fmt, 'legacy_fixed6_v1')
        self.assertIsNone(error)
        self.assertAlmostEqual(values['temperature'], -5.25)
        self.assertAlmostEqual(values['humidity'], 50.0)
        self.assertEqual(values['moisture'], 1234)

    def test_configured_custom_field_map(self):
        fields = build_codec_fields([{"id":"s1", "partId":"custom_module", "label":"Probe", "fields":[{"key":"conductivity_ms_cm", "label":"EC", "unit":"mS/cm"}]}])
        field = fields[0]
        raw = encode_emf1([(field['id'], field['typeCode'], 1234)])
        _fmt, values, error = decode_application_payload(raw, 10, fields)
        self.assertIsNone(error)
        self.assertAlmostEqual(values[field['key']], 12.34)

    def test_unknown_field_known_type_is_skippable(self):
        raw = encode_emf1([(0xE0, 0x11, 123), (1, 0x24, 2880)])
        values, error = decode_emf1(raw)
        self.assertIsNone(error)
        self.assertEqual(values['field_E0'], 123)
        self.assertAlmostEqual(values['temperature'], 28.8)

    def test_truncated_record_is_reported(self):
        _values, error = decode_emf1(bytes.fromhex('E1 01 24 0B'))
        self.assertIn('truncated', error.lower())

    def test_header_only_frame_is_rejected(self):
        _values, error = decode_emf1(bytes.fromhex('E1'))
        self.assertIn('header only', error.lower())

    def test_duplicate_field_id_is_rejected(self):
        _values, error = decode_emf1(bytes.fromhex('E1 01 24 0B 40 01 24 0B 40'))
        self.assertIn('duplicate field id', error.lower())

    def test_reserved_field_id_is_rejected(self):
        _values, error = decode_emf1(bytes.fromhex('E1 00 11 00 01'))
        self.assertIn('reserved field id', error.lower())

    def test_standard_field_type_is_enforced(self):
        _values, error = decode_emf1(bytes.fromhex('E1 01 11 00 01'))
        self.assertIn('registry requires', error.lower())

    def test_bool_range_is_checked(self):
        _values, error = decode_emf1(bytes.fromhex('E1 0D 01 02'))
        self.assertIn('must be 0 or 1', error)

    def test_legacy_requires_six_bytes(self):
        _values, error = decode_legacy_fixed6(bytes([1, 2, 3]))
        self.assertIn('exactly 6 bytes', error)

    def test_ids_are_unique_in_config(self):
        fields = build_codec_fields([{"id":"a", "fields":[{"key":"temperature"}]}, {"id":"b", "fields":[{"key":"temperature"}]}])
        self.assertEqual(fields[0]['id'], 1)
        self.assertNotEqual(fields[0]['id'], fields[1]['id'])
        self.assertEqual(fields[1]['key'], 'temperature_2')
        self.assertGreaterEqual(fields[1]['id'], 0x80)

    def test_custom_field_ids_survive_inserted_sensor(self):
        old = build_codec_fields([{"id":"stable-sensor", "fields":[{"key":"custom_ratio", "unit":"ratio"}]}])
        old_id = old[0]['id']
        edited = build_codec_fields([
            {"id":"new-sensor", "fields":[{"key":"new_custom", "unit":"unit"}]},
            {"id":"stable-sensor", "fields":[{"key":"custom_ratio", "unit":"ratio"}]},
        ], old)
        preserved = next(f for f in edited if f['sensorId'] == 'stable-sensor')
        self.assertEqual(preserved['id'], old_id)
        self.assertNotEqual(next(f for f in edited if f['sensorId'] == 'new-sensor')['id'], old_id)

    def test_existing_field_id_and_name_survive_duplicate_sensor_insertion(self):
        old = build_codec_fields([{"id":"stable-dht", "fields":[{"key":"temperature", "unit":"°C"}]}])
        existing_id = old[0]['id']
        edited = build_codec_fields([
            {"id":"new-dht", "fields":[{"key":"temperature", "unit":"°C"}]},
            {"id":"stable-dht", "fields":[{"key":"temperature", "unit":"°C"}]},
        ], old)
        preserved = next(f for f in edited if f['sensorId'] == 'stable-dht')
        inserted = next(f for f in edited if f['sensorId'] == 'new-dht')
        self.assertEqual(preserved['id'], existing_id)
        self.assertEqual(preserved['key'], 'temperature')
        self.assertNotEqual(inserted['id'], existing_id)
        self.assertEqual(inserted['key'], 'temperature_2')

    def test_existing_custom_field_survives_field_inserted_before_it(self):
        old = build_codec_fields([{"id":"stable-sensor", "fields":[{"key":"custom_ratio", "unit":"ratio"}]}])
        previous = old[0]
        edited = build_codec_fields([{"id":"stable-sensor", "fields":[{"key":"new_metric", "unit":"count"}, {"key":"custom_ratio", "unit":"ratio"}]}], old)
        preserved = next(f for f in edited if f['sourceKey'] == 'custom_ratio')
        self.assertEqual(preserved['id'], previous['id'])
        self.assertEqual(preserved['key'], previous['key'])

    def test_core_uptime_field_0x10_decodes(self):
        raw = encode_emf1([(0x10, 0x31, 3600)])
        fmt, values, error = decode_application_payload(raw, 10)
        self.assertEqual(fmt, 'edge_measurement_v1')
        self.assertIsNone(error)
        self.assertEqual(values['core_uptime_s'], 3600)

    def test_registry_schema_present(self):
        self.assertEqual(EMF1_REGISTRY.get('schema'), 'edge-monitor-emf1-registry-v1')
        self.assertTrue(EMF1_REGISTRY.get('legacyReceiveCompatibility', {}).get('enabled'))

    def test_validate_node_config_enode1_and_enode2_and_rejections(self):
        import json
        from pathlib import Path
        import edge_dash
        catalog = json.loads(Path(__file__).with_name('parts_catalog.json').read_text(encoding='utf-8'))

        enode1 = edge_dash.validate_node_config({
            "devEui": "d251f42dc4918f96",
            "joinEui": "827b193dd87983cf",
            "name": "ENode1",
            "otaApSsid": "ECOWAN-ENode1",
            "otaApPassword": "ECOWAN-ENode1",
            "reportingIntervalSeconds": 60,
            "utcSlotSecond": 5,
            "sensors": [
                {"id": "s1", "partId": "dht11", "protocol": "gpio", "port": "GPIO4", "fields": [{"key": "temperature"}, {"key": "humidity"}]},
                {"id": "s2", "partId": "hw038_moisture", "protocol": "analog", "port": "GPIO34", "fields": [{"key": "moisture"}]},
            ],
        }, catalog)
        self.assertEqual(enode1["utcSlotSecond"], 5)
        self.assertEqual(enode1["otaApSsid"], "ECOWAN-ENode1")

        enode2 = edge_dash.validate_node_config({
            "devEui": "b1e299ff3eed5d1d",
            "joinEui": "6bbf280ebd665410",
            "name": "ENode2",
            "otaApSsid": "ECOWAN-ENode2",
            "otaApPassword": "ECOWAN-ENode2",
            "reportingIntervalSeconds": 60,
            "utcSlotSecond": 35,
            "sensors": [
                {"id": "s1", "partId": "dht11", "protocol": "gpio", "port": "GPIO4", "fields": [{"key": "temperature"}, {"key": "humidity"}]},
                {"id": "s2", "partId": "hw038_moisture", "protocol": "analog", "port": "GPIO34", "fields": [{"key": "moisture"}]},
            ],
        }, catalog)
        self.assertEqual(enode2["utcSlotSecond"], 35)
        self.assertEqual(enode2["joinEui"], "6bbf280ebd665410")

        # Reject unknown part ID
        with self.assertRaisesRegex(ValueError, "Unknown part id"):
            edge_dash.validate_node_config({
                "devEui": "b1e299ff3eed5d1d", "name": "ENode2",
                "sensors": [{"partId": "non_existent_sensor", "protocol": "gpio", "port": "GPIO4"}],
            }, catalog)

        # Reject invalid protocol for part
        with self.assertRaisesRegex(ValueError, "Protocol i2c is not listed"):
            edge_dash.validate_node_config({
                "devEui": "b1e299ff3eed5d1d", "name": "ENode2",
                "sensors": [{"partId": "dht11", "protocol": "i2c", "port": "GPIO4"}],
            }, catalog)

        # Reject invalid devEui length/format
        with self.assertRaisesRegex(ValueError, "DevEUI must be exactly 16 hexadecimal"):
            edge_dash.validate_node_config({
                "devEui": "invalid_eui", "name": "ENode2",
            }, catalog)

        # Reject password shorter than 8 chars
        with self.assertRaisesRegex(ValueError, "Password must be at least 8"):
            edge_dash.validate_node_config({
                "devEui": "b1e299ff3eed5d1d", "name": "ENode2",
                "otaApPassword": "short",
            }, catalog)

        # Reject reporting interval out of range
        with self.assertRaisesRegex(ValueError, "between 10 and 86400"):
            edge_dash.validate_node_config({
                "devEui": "b1e299ff3eed5d1d", "name": "ENode2",
                "reportingIntervalSeconds": 5,
            }, catalog)

        # Reject generating legacy Fixed6
        with self.assertRaisesRegex(ValueError, "receive-only"):
            edge_dash.validate_node_config({
                "devEui": "b1e299ff3eed5d1d", "name": "ENode2",
                "payloadCodec": "legacy_fixed6_v1",
            }, catalog)


class EgdcV2CodecAndRadioTests(unittest.TestCase):
    """Stage 1 TDD Contract: EGDC v2 Wire Codec, App/MAC/PHY Radio Calculator, and Cutoff Policy."""

    def test_egdc_constants_and_crc8_vectors(self):
        import payload_codec
        self.assertEqual(payload_codec.EGDC_MAGIC, 0xE2)
        self.assertEqual(payload_codec.EGDC_FPORT, 11)
        self.assertEqual(payload_codec.EGDC_GRID_W, 32)
        self.assertEqual(payload_codec.EGDC_GRID_H, 24)
        self.assertEqual(payload_codec.EGDC_MAX_CELLS, 768)
        self.assertEqual(payload_codec.crc8_egdc(b""), 0x00)
        c1 = payload_codec.crc8_egdc(b"\xE2\x01\x02\x00\x00")
        c2 = payload_codec.crc8_egdc(b"\xE2\x01\x02\x00\x01")
        self.assertIsInstance(c1, int)
        self.assertTrue(0 <= c1 <= 0xFF)
        self.assertNotEqual(c1, c2)

    def test_egdc_mode0_mode1_mode2_round_trip_and_fport11_dispatch(self):
        import payload_codec
        cells_in = [
            {
                "cell_idx": 0,
                "mode": 0,
                "priority": 1,
                "rgb565": 0x1234,
            },
            {
                "cell_idx": 345,
                "mode": 1,
                "priority": 4,
                "luma_4x4": [i for i in range(16)],
            },
            {
                "cell_idx": 767,
                "mode": 2,
                "priority": 7,
                "gradient_8x8": [10 + i for i in range(16)],
                "chroma_anchor": 0xABCD,
            },
        ]
        raw = payload_codec.encode_egdc_packet(
            keyframe_gen_id=7,
            frame_seq=42,
            pkt_idx=0,
            pkt_total=2,
            cells=cells_in,
            flags=0x05,
        )
        # 6B header + 4B (Mode 0) + 10B (Mode 1) + 20B (Mode 2) = 40 bytes
        self.assertEqual(len(raw), 40)
        self.assertEqual(raw[0], 0xE2)

        decoded, err = payload_codec.decode_egdc_packet(raw)
        self.assertIsNone(err)
        self.assertEqual(decoded["keyframe_gen_id"], 7)
        self.assertEqual(decoded["frame_seq"], 42)
        self.assertEqual(decoded["pkt_idx"], 0)
        self.assertEqual(decoded["pkt_total"], 2)
        self.assertEqual(decoded["flags"], 0x05)
        self.assertEqual(len(decoded["cells"]), 3)

        c0, c1, c2 = decoded["cells"]
        self.assertEqual(c0["cell_idx"], 0)
        self.assertEqual(c0["mode"], 0)
        self.assertEqual(c0["priority"], 1)
        self.assertEqual(c0["rgb565"], 0x1234)

        self.assertEqual(c1["cell_idx"], 345)
        self.assertEqual(c1["mode"], 1)
        self.assertEqual(c1["priority"], 4)
        self.assertEqual(c1["luma_4x4"], [i for i in range(16)])

        self.assertEqual(c2["cell_idx"], 767)
        self.assertEqual(c2["mode"], 2)
        self.assertEqual(c2["priority"], 7)
        self.assertEqual(c2["gradient_8x8"], [10 + i for i in range(16)])
        self.assertEqual(c2["chroma_anchor"], 0xABCD)

        # Verify decode_application_payload dispatches FPort 11 to EGDC v2
        fmt, app_decoded, app_err = decode_application_payload(raw, 11)
        self.assertEqual(fmt, "egdc_v2")
        self.assertIsNone(app_err)
        self.assertEqual(app_decoded["frame_seq"], 42)

    def test_egdc_rejects_malformed_corrupt_and_out_of_range_packets(self):
        import payload_codec
        valid = payload_codec.encode_egdc_packet(
            keyframe_gen_id=1,
            frame_seq=1,
            pkt_idx=0,
            pkt_total=1,
            cells=[{"cell_idx": 10, "mode": 0, "priority": 1, "rgb565": 0x00FF}],
        )

        # 1. Truncated header (< 6 bytes)
        _, err = payload_codec.decode_egdc_packet(valid[:4])
        self.assertIsNotNone(err)
        self.assertIn("truncated", err.lower())

        # 2. Invalid magic byte
        bad_magic = bytearray(valid)
        bad_magic[0] = 0xE1
        _, err = payload_codec.decode_egdc_packet(bytes(bad_magic))
        self.assertIsNotNone(err)

        # 3. CRC-8 mismatch (flip a bit in payload)
        corrupt = bytearray(valid)
        corrupt[-1] ^= 0xFF
        _, err = payload_codec.decode_egdc_packet(bytes(corrupt))
        self.assertIsNotNone(err)
        self.assertIn("crc", err.lower())

        # 4. Invalid packet index >= packet total in header byte 3
        bad_idx = bytearray(valid)
        # pkt_idx = 3 (high nibble), pkt_total - 1 = 1 (low nibble -> pkt_total = 2)
        bad_idx[3] = (3 << 4) | 0x01
        bad_idx[5] = payload_codec.crc8_egdc(bytes(bad_idx[:5]) + bytes(bad_idx[6:]))
        _, err = payload_codec.decode_egdc_packet(bytes(bad_idx))
        self.assertIsNotNone(err)

        # 5. Out-of-range cell_idx >= 768
        with self.assertRaises(ValueError):
            payload_codec.encode_egdc_packet(
                keyframe_gen_id=1,
                frame_seq=1,
                pkt_idx=0,
                pkt_total=1,
                cells=[{"cell_idx": 768, "mode": 0, "priority": 1, "rgb565": 0}],
            )

        # Crafted wire packet with cell_idx = 800 and valid CRC-8
        cell_hdr = ((800 & 0x3FF) << 6) | (0 << 4) | 1
        crafted_body = cell_hdr.to_bytes(2, "big") + b"\x00\x00"
        hdr_prefix = b"\xE2\x01\x01\x00\x00"
        crc = payload_codec.crc8_egdc(hdr_prefix + crafted_body)
        _, err = payload_codec.decode_egdc_packet(hdr_prefix + bytes([crc]) + crafted_body)
        self.assertIsNotNone(err)

        # 6. Truncated cell record payload
        trunc_body = valid[:-1]
        hdr_p = trunc_body[:5]
        re_crc = payload_codec.crc8_egdc(hdr_p + trunc_body[6:])
        _, err = payload_codec.decode_egdc_packet(hdr_p + bytes([re_crc]) + trunc_body[6:])
        self.assertIsNotNone(err)
        self.assertIn("truncated", err.lower())

    def test_pack_egdc_frames_orders_by_priority_and_splits_packets(self):
        import payload_codec
        cells = [
            {"cell_idx": 1, "mode": 0, "priority": 1, "rgb565": 0x1111},
            {"cell_idx": 2, "mode": 2, "priority": 7, "gradient_8x8": [1] * 16, "chroma_anchor": 0x2222},
            {"cell_idx": 3, "mode": 1, "priority": 5, "luma_4x4": [2] * 16},
        ]
        # With max_app_bytes = 26 (6B header + 20B), Mode 2 cell takes packet 0 alone,
        # and Mode 1 (10B) + Mode 0 (4B) + 6B header = 20B fit in packet 1.
        packets = payload_codec.pack_egdc_frames(
            keyframe_gen_id=3,
            frame_seq=9,
            cells=cells,
            max_app_bytes=26,
        )
        self.assertEqual(len(packets), 2)
        d0, err0 = payload_codec.decode_egdc_packet(packets[0])
        d1, err1 = payload_codec.decode_egdc_packet(packets[1])
        self.assertIsNone(err0)
        self.assertIsNone(err1)
        self.assertEqual(d0["pkt_idx"], 0)
        self.assertEqual(d0["pkt_total"], 2)
        self.assertEqual(d0["cells"][0]["cell_idx"], 2)  # Highest priority Mode 2 first
        self.assertEqual(d1["pkt_idx"], 1)
        self.assertEqual(d1["pkt_total"], 2)
        self.assertEqual([c["cell_idx"] for c in d1["cells"]], [3, 1])

    def test_lora_airtime_and_as923_app_mac_phy_separation(self):
        import payload_codec
        # DR5 (SF7BW125) with 160B App + 13B LoRaWAN MAC = 173B PHY -> 276.736 ms
        res_160 = payload_codec.calculate_lora_airtime(app_bytes=160, sf=7, bw_khz=125, mac_overhead_bytes=13)
        self.assertEqual(res_160["app_bytes"], 160)
        self.assertEqual(res_160["mac_bytes"], 13)
        self.assertEqual(res_160["phy_bytes"], 173)
        self.assertAlmostEqual(res_160["airtime_ms"], 276.736, places=2)
        self.assertTrue(res_160["dwell_400ms_ok"])

        # Custom Raw LoRa (2B MAC header) vs LoRaWAN (13B MAC header) for 51B App at SF7
        raw_51 = payload_codec.calculate_lora_airtime(app_bytes=51, sf=7, bw_khz=125, mac_overhead_bytes=2)
        lorawan_51 = payload_codec.calculate_lora_airtime(app_bytes=51, sf=7, bw_khz=125, mac_overhead_bytes=13)
        self.assertEqual(raw_51["phy_bytes"], 53)
        self.assertEqual(lorawan_51["phy_bytes"], 64)
        self.assertAlmostEqual(lorawan_51["airtime_ms"], 118.016, places=2)
        self.assertLess(raw_51["airtime_ms"], lorawan_51["airtime_ms"])

        # AS923 DR2 (SF10BW125): 51B App exceeds 400ms dwell limit (698.368 ms) when dwell_time_limited=True
        dr2_dwell = payload_codec.check_as923_payload_limit(app_bytes=51, dr=2, dwell_time_limited=True)
        self.assertFalse(dr2_dwell["valid"])
        self.assertFalse(dr2_dwell["dwell_400ms_ok"])
        self.assertAlmostEqual(dr2_dwell["airtime_ms"], 698.368, places=2)

        # AS923 DR2 with Dwell=0 allows 51B App
        dr2_no_dwell = payload_codec.check_as923_payload_limit(app_bytes=51, dr=2, dwell_time_limited=False)
        self.assertTrue(dr2_no_dwell["valid"])

        # AS923 DR5 (SF7BW125): 222B App is valid (368.896 ms <= 400 ms), 223B App is rejected
        dr5_222 = payload_codec.check_as923_payload_limit(app_bytes=222, dr=5, dwell_time_limited=True)
        self.assertTrue(dr5_222["valid"])
        self.assertAlmostEqual(dr5_222["airtime_ms"], 368.896, places=2)
        dr5_223 = payload_codec.check_as923_payload_limit(app_bytes=223, dr=5, dwell_time_limited=True)
        self.assertFalse(dr5_223["valid"])

    def test_adaptive_cutoff_profiles_and_cost_model(self):
        import payload_codec
        # Conservative profile (max 4 packets): 4 packets -> lora_delta, 5 packets -> keyframe
        c_ok = payload_codec.evaluate_cutoff_policy(packets_required=4, profile="conservative")
        self.assertEqual(c_ok["action"], "lora_delta")
        self.assertEqual(c_ok["max_packets"], 4)

        c_over = payload_codec.evaluate_cutoff_policy(packets_required=5, profile="conservative")
        self.assertEqual(c_over["action"], "keyframe")
        self.assertTrue(c_over["exceeded_packet_cap"])

        # Balanced profile (max 8 packets), Extended (10), Experimental (16)
        self.assertEqual(payload_codec.evaluate_cutoff_policy(8, profile="balanced")["action"], "lora_delta")
        self.assertEqual(payload_codec.evaluate_cutoff_policy(9, profile="balanced")["action"], "keyframe")
        self.assertEqual(payload_codec.evaluate_cutoff_policy(10, profile="extended")["action"], "lora_delta")
        self.assertEqual(payload_codec.evaluate_cutoff_policy(16, profile="experimental", keyframe_cost_ms=9999.0)["action"], "lora_delta")

        # Cost-based fallback even when packet count <= profile cap
        cost_over = payload_codec.evaluate_cutoff_policy(
            packets_required=6,
            profile="balanced",
            airtime_per_pkt_ms=276.7,
            queue_wait_ms=800.0,
            recovery_overhead_ms=500.0,
            keyframe_cost_ms=2000.0,
        )
        self.assertEqual(cost_over["action"], "keyframe")
        self.assertFalse(cost_over["exceeded_packet_cap"])
        self.assertTrue(cost_over["exceeded_cost_cap"])


if __name__ == '__main__':
    unittest.main()
