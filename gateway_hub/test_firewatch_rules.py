"""Unit tests for the Firewatch RGB Rule Engine, Foveated ROI Block Slicer, and USB LoRa Bridge.

TDD Contract (Written BEFORE implementation):
1. Sky vs. Canopy context segmentation on 32x24 grid.
2. Color priority classification: Red (flame), Grey (smoke), Black (carbon smoke) vs. Normal.
3. Sky-surrounded gating: Grey/Black only qualify when surrounded by or bordering Sky; ground shadows rejected.
4. Upward-rising kinematic rule: Grey/Black must rise upward (row index decreasing over frames) to trigger;
   static or horizontally drifting clouds/shadows do not trigger.
5. Foveated 20x20 native pixel block slicing from 640x480 frame and Orin canvas stitching.
6. USB LoRa Extension serial command framing and ACK parsing.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _make_firewatch_scene(cols: int = 32, rows: int = 24) -> list[tuple[int, int, int]]:
    """Create a baseline 32x24 firewatch scene:
    - Top rows 0..11: Sky blue (130, 185, 235)
    - Bottom rows 12..23: Forest canopy green (35, 85, 40)
    """
    grid = []
    for r in range(rows):
        for c in range(cols):
            if r < 12:
                grid.append((130, 185, 235))
            else:
                grid.append((35, 85, 40))
    return grid


class TestFirewatchRules(unittest.TestCase):
    def test_sky_mask_segments_sky_vs_forest_canopy(self):
        """Top sky rows must be marked True; lower forest canopy rows must be marked False."""
        from vision_engine import FirewatchRuleEngine

        scene = _make_firewatch_scene()
        engine = FirewatchRuleEngine(cols=32, rows=24)
        sky_mask = engine.build_sky_mask(scene)

        self.assertEqual(len(sky_mask), 32 * 24)
        # Row 2, Col 10 is deep in the sky -> True
        self.assertTrue(sky_mask[2 * 32 + 10])
        # Row 10, Col 15 is sky just above horizon -> True
        self.assertTrue(sky_mask[10 * 32 + 15])
        # Row 18, Col 15 is deep in the forest canopy -> False
        self.assertFalse(sky_mask[18 * 32 + 15])

    def test_color_priority_classifies_red_grey_black_and_ignores_normal(self):
        """Color classifier must prioritize Red, Grey, and Black while ignoring normal green/blue."""
        from vision_engine import FirewatchRuleEngine

        engine = FirewatchRuleEngine(cols=32, rows=24)
        # Active flame core: bright red/orange
        self.assertEqual(engine.classify_color((235, 55, 20), ref_rgb=(35, 85, 40)), "red")
        # Ash/steam smoke: medium-bright desaturated grey with luma shift from reference
        self.assertEqual(engine.classify_color((165, 168, 162), ref_rgb=(130, 185, 235)), "grey")
        # Dense carbon smoke: dark desaturated black/charcoal against sky reference
        self.assertEqual(engine.classify_color((38, 40, 37), ref_rgb=(130, 185, 235)), "black")
        # Normal forest green and sky blue
        self.assertEqual(engine.classify_color((38, 88, 42), ref_rgb=(35, 85, 40)), "normal")
        self.assertEqual(engine.classify_color((128, 182, 232), ref_rgb=(130, 185, 235)), "normal")

    def test_grey_and_black_require_sky_context_while_red_triggers_anywhere(self):
        """Grey/black on forest floor (ground shadow) must be rejected; grey/black in/near sky accepted."""
        from vision_engine import FirewatchRuleEngine

        scene = _make_firewatch_scene()
        engine = FirewatchRuleEngine(cols=32, rows=24)
        engine.initialize_reference(scene)

        # Cell in deep forest floor (row 19, col 10) surrounded only by canopy
        ground_cell = 19 * 32 + 10
        self.assertFalse(engine.has_sky_context(ground_cell))

        # Cell at sky boundary / in sky (row 10, col 10) surrounded by sky
        sky_cell = 10 * 32 + 10
        self.assertTrue(engine.has_sky_context(sky_cell))

        # Cell right at canopy crest bordering row 11 sky (row 12, col 10)
        crest_cell = 12 * 32 + 10
        self.assertTrue(engine.has_sky_context(crest_cell))

    def test_ground_shadow_and_horizontal_cloud_do_not_trigger_alert(self):
        """A dark/grey shadow on the ground or moving purely horizontally must NOT trigger an alert."""
        from vision_engine import FirewatchRuleEngine

        base = _make_firewatch_scene()
        engine = FirewatchRuleEngine(cols=32, rows=24)
        engine.initialize_reference(base)

        # Frame 1..3: Ground shadow sweeps horizontally across forest floor (row 18, cols 10 -> 11 -> 12)
        # AND a cloud drifts purely horizontally in the sky (row 6, cols 5 -> 6 -> 7) without rising
        for step in range(3):
            frame = list(base)
            # Ground shadow (black/grey on forest floor)
            frame[18 * 32 + (10 + step)] = (30, 32, 30)
            # Horizontal cloud in sky (same row 6, moving sideways)
            frame[6 * 32 + (5 + step)] = (160, 162, 160)
            res = engine.process_rgb_grid(frame)

        self.assertFalse(res["alert"])
        self.assertEqual(res["triggered_cells"], [])

    def test_rising_grey_or_black_smoke_in_sky_triggers_alert(self):
        """Grey/black plume rising upward (row 11 -> row 10 -> row 9) in sky context MUST trigger."""
        from vision_engine import FirewatchRuleEngine

        base = _make_firewatch_scene()
        engine = FirewatchRuleEngine(cols=32, rows=24)
        engine.initialize_reference(base)

        # Frame 1: Smoke emerges at horizon (row 11, col 16)
        f1 = list(base)
        f1[11 * 32 + 16] = (160, 162, 158)  # Grey smoke
        r1 = engine.process_rgb_grid(f1)
        self.assertFalse(r1["alert"])  # Not yet proven rising on first frame

        # Frame 2: Smoke rises upward into row 10, col 16 (and base darkens to black smoke)
        f2 = list(f1)
        f2[11 * 32 + 16] = (42, 44, 40)    # Black smoke at base
        f2[10 * 32 + 16] = (162, 164, 160) # Grey smoke rising above it
        r2 = engine.process_rgb_grid(f2)

        # Frame 3: Smoke continues rising into row 9, col 16
        f3 = list(f2)
        f3[9 * 32 + 16] = (168, 170, 166)
        r3 = engine.process_rgb_grid(f3)

        self.assertTrue(r3["alert"])
        self.assertEqual(r3["alert_type"], "rising_smoke")
        self.assertIn(9 * 32 + 16, r3["triggered_cells"])
        self.assertIn(10 * 32 + 16, r3["triggered_cells"])

    def test_red_flame_triggers_immediately(self):
        """Bright red flame core must trigger immediately on detection."""
        from vision_engine import FirewatchRuleEngine

        base = _make_firewatch_scene()
        engine = FirewatchRuleEngine(cols=32, rows=24)
        engine.initialize_reference(base)

        f1 = list(base)
        flame_cell = 12 * 32 + 20
        f1[flame_cell] = (245, 50, 15)  # Red/orange fire core
        res = engine.process_rgb_grid(f1)

        self.assertTrue(res["alert"])
        self.assertEqual(res["alert_type"], "red_flame")
        self.assertIn(flame_cell, res["triggered_cells"])

    def test_foveated_roi_pixel_block_slicer_and_orin_stitcher(self):
        """Triggered 32x24 cells must slice exact 20x20 pixel blocks from 640x480 and stitch cleanly on Orin."""
        from vision_engine import slice_roi_blocks, stitch_roi_blocks

        width, height = 640, 480
        # Reference frame: all zeros (black)
        ref_frame = bytes(width * height * 3)

        # Live frame: put bright red/grey pattern inside cell (row=9, col=16 -> idx=304)
        # Cell (9, 16) spans x=[320..339], y=[180..199]
        live_buf = bytearray(width * height * 3)
        target_cell = 9 * 32 + 16
        for y in range(180, 200):
            for x in range(320, 340):
                off = (y * width + x) * 3
                live_buf[off] = 220
                live_buf[off + 1] = 40
                live_buf[off + 2] = 30

        blocks = slice_roi_blocks(bytes(live_buf), width, height, [target_cell], cols=32, rows=24)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["cell_idx"], target_cell)
        self.assertEqual(len(blocks[0]["rgb_bytes"]), 20 * 20 * 3)

        # Stitch onto reference frame on the Orin side
        stitched = stitch_roi_blocks(ref_frame, width, height, blocks, cols=32, rows=24)
        self.assertEqual(len(stitched), width * height * 3)

        # Verify pixel inside stitched block (x=325, y=185) matches live frame
        check_off = (185 * width + 325) * 3
        self.assertEqual(stitched[check_off], 220)
        self.assertEqual(stitched[check_off + 1], 40)
        self.assertEqual(stitched[check_off + 2], 30)

        # Verify untouched pixel outside block (x=10, y=10) remains 0
        self.assertEqual(stitched[(10 * width + 10) * 3], 0)

    def test_usb_lora_serial_bridge_framing_and_ack(self):
        """USB LoRa extension serial framing must wrap EGDC bytes into AT+LORA_TX hex command and parse ACK."""
        from vision_transport import format_usb_lora_frame, parse_usb_lora_ack

        raw_egdc = bytes([0xE2, 0x01, 0x05, 0x00, 0x02, 0x9A, 0xAA, 0xBB])
        serial_line = format_usb_lora_frame(raw_egdc, fport=11)
        self.assertEqual(serial_line, "AT+LORA_TX=11:e2010500029aaabb\n")

        ack = parse_usb_lora_ack("OK tx_done fport=11 bytes=8 airtime_ms=169.2\n")
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["fport"], 11)
        self.assertAlmostEqual(ack["airtime_ms"], 169.2, places=1)

    def test_usb_lora_bridge_client_dry_run_and_simulated_dispatch(self):
        """UsbLoRaBridgeClient in dry_run mode must format AT command, simulate airtime, and return success."""
        from vision_transport import UsbLoRaBridgeClient

        client = UsbLoRaBridgeClient(port="AUTO", dry_run=True)
        self.assertTrue(client.connect())
        self.assertTrue(client.ping())

        dummy_packet = bytes([0xE2, 0x01, 0x0A, 0x00, 0x01, 0x55, 0x66])
        res = client.send_packet(dummy_packet, fport=11)
        self.assertTrue(res["ok"])
        self.assertTrue(res["sent"])
        self.assertEqual(res["bytes"], len(dummy_packet))
        self.assertGreater(res["airtime_ms"], 0.0)
        self.assertEqual(res["raw_cmd"], "AT+LORA_TX=11:e2010a00015566")
        client.close()



if __name__ == "__main__":
    unittest.main()
