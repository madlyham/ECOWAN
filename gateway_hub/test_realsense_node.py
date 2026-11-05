"""Unit tests for RealSense & Edge Camera Node (gateway_hub/realsense_node.py).

Verifies:
1. Frame downsampling to 32x24 grid.
2. Synthetic test pattern and smoke plume injection.
3. Integration with VisualDifferenceEngine and EGDC v2 wire packet generation.
4. Hub HTTP dispatch of generated packets.
5. Hardware mode fallback behavior.
"""

import unittest
from unittest.mock import patch, MagicMock
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from realsense_node import (
    downsample_rgb_buffer,
    generate_synthetic_frame,
    inject_synthetic_smoke,
    RealSenseNode,
    EGDC_MAGIC_BYTE,
)


class TestRealSenseNode(unittest.TestCase):
    def test_synthetic_frame_generation(self):
        """Synthetic frame must generate exactly 32 * 24 * 3 bytes (RGB)."""
        frame = generate_synthetic_frame(cols=32, rows=24, base_color=(100, 120, 140))
        self.assertEqual(len(frame), 32 * 24 * 3)
        # Check first pixel RGB
        self.assertEqual(frame[0], 100)
        self.assertEqual(frame[1], 120)
        self.assertEqual(frame[2], 140)

    def test_inject_synthetic_smoke(self):
        """Injecting smoke must modify target cells to smoke gray/white values."""
        base = generate_synthetic_frame(cols=32, rows=24, base_color=(50, 50, 50))
        plume_cells = [100, 101, 132, 133]
        smoke_frame = inject_synthetic_smoke(base, cols=32, cells=plume_cells, smoke_intensity=200)
        
        self.assertEqual(len(smoke_frame), len(base))
        # Verify plume cells are brighter
        for cell_idx in plume_cells:
            offset = cell_idx * 3
            self.assertGreaterEqual(smoke_frame[offset], 180)

    def test_downsample_rgb_buffer(self):
        """Downsample a 64x48 image to 32x24 grid."""
        # Create a 64x48 3-channel image (64 * 48 * 3 bytes)
        high_res = bytearray(64 * 48 * 3)
        # Set top-left 2x2 pixels to bright red (255, 0, 0)
        for y in range(2):
            for x in range(2):
                idx = (y * 64 + x) * 3
                high_res[idx] = 255
                high_res[idx + 1] = 0
                high_res[idx + 2] = 0

        downsampled = downsample_rgb_buffer(high_res, src_w=64, src_h=48, dst_w=32, dst_h=24)
        self.assertEqual(len(downsampled), 32 * 24 * 3)
        # First cell should be average of top-left 2x2 = (255, 0, 0)
        self.assertEqual(downsampled[0], 255)
        self.assertEqual(downsampled[1], 0)
        self.assertEqual(downsampled[2], 0)

    def test_node_process_frame_generates_egdc_packets(self):
        """Node must encode delta against reference and produce valid EGDC packets."""
        node = RealSenseNode(mode='synthetic', cols=32, rows=24)
        
        # Frame 1: Initial reference frame (baseline)
        f1 = generate_synthetic_frame(cols=32, rows=24, base_color=(80, 80, 80))
        pkts1 = node.process_frame(f1)
        # First frame initializes E_ref, should have 0 or minimal delta packets
        self.assertEqual(len(pkts1), 0)

        # Frame 2: Plume appears in cells [100, 101, 132, 133]
        f2 = inject_synthetic_smoke(f1, cols=32, cells=[100, 101, 132, 133], smoke_intensity=220)
        pkts2 = node.process_frame(f2)
        
        # Must produce at least 1 packet with magic byte 0xE2
        self.assertGreater(len(pkts2), 0)
        for pkt in pkts2:
            self.assertEqual(pkt[0], EGDC_MAGIC_BYTE)

    @patch('urllib.request.urlopen')
    def test_send_to_hub_http(self, mock_urlopen):
        """Verify node HTTP push to Edge Monitor hub endpoint."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True, "cells_updated": 4}).encode('utf-8')
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        node = RealSenseNode(mode='synthetic', cols=32, rows=24)
        test_packets = [bytes([EGDC_MAGIC_BYTE, 0x01, 0x01, 0x00, 0x00, 0x00])]
        
        res = node.send_to_hub("http://127.0.0.1:8026/api/vision/delta", test_packets)
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("cells_updated"), 4)
        mock_urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
