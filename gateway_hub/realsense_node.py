"""ECOWAN Hybrid Vision - RealSense & Edge Camera Node (realsense_node.py)

Captures frames from:
1. Intel RealSense D400-series (via pyrealsense2 pipeline)
2. Standard USB / V4L2 Webcams (via OpenCV VideoCapture)
3. Synthetic test-pattern generator with controllable smoke-plume injection

Downsamples to 32x24 grid, executes the VisualDifferenceEngine, encodes
compressed EGDC v2 wire packets (FPort 11, Magic 0xE2), and transmits deltas
to the Edge Monitor Hub over HTTP or serial LoRa.
"""

import sys
import os
import time
import json
import urllib.request
import urllib.error

# Ensure gateway_hub is on sys.path for internal imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vision_engine import VisualDifferenceEngine
from payload_codec import EGDC_MAGIC as EGDC_MAGIC_BYTE


def rgb_to_cell_dict(r, g, b, variance=0.0):
    """Convert RGB byte values to cell dict for VisualDifferenceEngine."""
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    r5 = (r >> 3) & 0x1F
    g6 = (g >> 2) & 0x3F
    b5 = (b >> 3) & 0x1F
    rgb565 = (r5 << 11) | (g6 << 5) | b5
    return {
        "luma": float(luma),
        "rgb565": int(rgb565),
        "variance": float(variance),
        "subcell_luma": [int(luma)] * 16,
        "subcell_grads": [0] * 16
    }


def generate_synthetic_frame(cols=32, rows=24, base_color=(100, 120, 140)):
    """Generate a uniform synthetic RGB buffer of size cols * rows * 3."""
    total_cells = cols * rows
    buf = bytearray(total_cells * 3)
    r, g, b = base_color
    for i in range(total_cells):
        off = i * 3
        buf[off] = r
        buf[off + 1] = g
        buf[off + 2] = b
    return bytes(buf)


def inject_synthetic_smoke(base_buffer, cols=32, cells=None, smoke_intensity=200):
    """Overlay a brighter/gray smoke signature onto specified cell indices."""
    if cells is None:
        cells = [100, 101, 132, 133]
    buf = bytearray(base_buffer)
    for c in cells:
        off = c * 3
        if off + 2 < len(buf):
            buf[off] = smoke_intensity
            buf[off + 1] = smoke_intensity
            buf[off + 2] = smoke_intensity
    return bytes(buf)


def downsample_rgb_buffer(src_buf, src_w, src_h, dst_w=32, dst_h=24):
    """Downsample an arbitrary RGB buffer to dst_w x dst_h using area averaging."""
    out = bytearray(dst_w * dst_h * 3)
    
    x_scale = src_w / dst_w
    y_scale = src_h / dst_h
    
    for dy in range(dst_h):
        sy_start = int(dy * y_scale)
        sy_end = max(sy_start + 1, int((dy + 1) * y_scale))
        for dx in range(dst_w):
            sx_start = int(dx * x_scale)
            sx_end = max(sx_start + 1, int((dx + 1) * x_scale))
            
            r_sum = 0
            g_sum = 0
            b_sum = 0
            count = 0
            
            for y in range(sy_start, min(sy_end, src_h)):
                row_off = y * src_w * 3
                for x in range(sx_start, min(sx_end, src_w)):
                    p_off = row_off + x * 3
                    r_sum += src_buf[p_off]
                    g_sum += src_buf[p_off + 1]
                    b_sum += src_buf[p_off + 2]
                    count += 1
            
            if count == 0:
                count = 1
                
            out_off = (dy * dst_w + dx) * 3
            out[out_off] = r_sum // count
            out[out_off + 1] = g_sum // count
            out[out_off + 2] = b_sum // count
            
    return bytes(out)


class RealSenseNode:
    """Stationary edge camera node managing sensor capture, delta encoding, and transmission."""
    
    def __init__(self, mode='auto', cols=32, rows=24, dev_index=0):
        self.cols = cols
        self.rows = rows
        self.dev_index = dev_index
        self.engine = None
        self.keyframe_gen_id = 1
        self.frame_seq = 0
        self.backend = None
        self._init_backend(mode)
        
    def _init_backend(self, mode):
        """Initialize the requested or best-available video capture backend."""
        if mode == 'realsense':
            if self._try_realsense():
                self.backend = 'realsense'
                return
            raise RuntimeError("Intel RealSense requested but hardware or pyrealsense2 unavailable")
            
        elif mode == 'opencv':
            if self._try_opencv():
                self.backend = 'opencv'
                return
            raise RuntimeError("OpenCV requested but camera device or cv2 unavailable")
            
        elif mode == 'synthetic':
            self.backend = 'synthetic'
            return
            
        # 'auto' mode fallback cascade
        if self._try_realsense():
            self.backend = 'realsense'
        elif self._try_opencv():
            self.backend = 'opencv'
        else:
            self.backend = 'synthetic'

    def _try_realsense(self):
        try:
            import pyrealsense2 as rs
            pipeline = rs.pipeline()
            config = rs.config()
            config.enable_stream(rs.stream.color, 640, 480, rs.format.rgb8, 15)
            pipeline.start(config)
            self._rs_pipeline = pipeline
            return True
        except Exception:
            return False

    def _try_opencv(self):
        try:
            import cv2
            cap = cv2.VideoCapture(self.dev_index)
            if cap.isOpened():
                ret, _ = cap.read()
                if ret:
                    self._cv_cap = cap
                    return True
                cap.release()
            return False
        except Exception:
            return False

    def capture_frame(self):
        """Capture one raw RGB frame from the active backend."""
        if self.backend == 'realsense':
            frames = self._rs_pipeline.wait_for_frames()
            color_frame = frames.get_color_frame()
            if not color_frame:
                return generate_synthetic_frame(self.cols, self.rows)
            import numpy as np
            arr = np.asanyarray(color_frame.get_data())
            return downsample_rgb_buffer(arr.tobytes(), 640, 480, self.cols, self.rows)
            
        elif self.backend == 'opencv':
            ret, frame = self._cv_cap.read()
            if not ret:
                return generate_synthetic_frame(self.cols, self.rows)
            import cv2
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w = rgb.shape[:2]
            return downsample_rgb_buffer(rgb.tobytes(), w, h, self.cols, self.rows)
            
        else:
            return generate_synthetic_frame(self.cols, self.rows)

    def process_frame(self, frame_rgb=None, profile="balanced", max_app_bytes=222):
        """Process an RGB frame, detect visual changes, and generate wire packets."""
        if frame_rgb is None:
            frame_rgb = self.capture_frame()
            
        self.frame_seq += 1
        
        # Build list of cell dicts for VisualDifferenceEngine
        cell_count = self.cols * self.rows
        current_cells = []
        for i in range(cell_count):
            off = i * 3
            r = frame_rgb[off]
            g = frame_rgb[off + 1]
            b = frame_rgb[off + 2]
            # If high brightness/smoke, attach variance for Mode 2
            variance = 20.0 if (r > 180 and g > 180 and b > 180) else 0.0
            current_cells.append(rgb_to_cell_dict(r, g, b, variance=variance))
            
        # First frame initializes engine reference
        if self.engine is None:
            self.engine = VisualDifferenceEngine(
                keyframe_cells=current_cells,
                keyframe_gen_id=self.keyframe_gen_id
            )
            return []
            
        # Run delta pipeline
        res = self.engine.process_frame(
            current_cells=current_cells,
            frame_seq=self.frame_seq,
            profile=profile,
            max_app_bytes=max_app_bytes
        )
        return res.get("packets", [])

    def send_to_hub(self, hub_url, packets):
        """Transmit encoded EGDC packets to Edge Monitor hub over HTTP."""
        if not packets:
            return {"ok": True, "packets_sent": 0}
            
        payload = {
            "keyframe_gen_id": self.keyframe_gen_id,
            "frame_seq": self.frame_seq,
            "packets": [p.hex() for p in packets],
            "timestamp": time.time(),
            "backend": self.backend
        }
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            hub_url,
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            body = response.read().decode('utf-8')
            return json.loads(body)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ECOWAN Edge Vision Camera Node")
    parser.add_argument("--hub", default="http://100.83.203.103:8026/api/vision/delta", help="Dashboard delta endpoint")
    parser.add_argument("--mode", default="auto", choices=["auto", "realsense", "opencv", "synthetic"])
    parser.add_argument("--smoke", action="store_true", help="Inject synthetic smoke plume")
    parser.add_argument("--fps", type=float, default=1.0, help="Frame analysis rate in Hz")
    parser.add_argument("--once", action="store_true", help="Run single frame then exit")
    args = parser.parse_args()

    print(f"Initializing ECOWAN Vision Node (mode={args.mode})...")
    node = RealSenseNode(mode=args.mode)
    print(f"Backend active: {node.backend}")

    # Baseline frame
    f1 = node.capture_frame()
    node.process_frame(f1)
    print("Baseline reference established.")

    while True:
        if args.smoke:
            frame = inject_synthetic_smoke(node.capture_frame())
        else:
            frame = node.capture_frame()
            
        pkts = node.process_frame(frame)
        if pkts:
            print(f"Frame {node.frame_seq}: {len(pkts)} delta packets generated ({sum(len(p) for p in pkts)} B).")
            try:
                res = node.send_to_hub(args.hub, pkts)
                print(f"  -> Dispatched to hub: {res}")
            except Exception as e:
                print(f"  -> Hub push failed: {e}")
        else:
            print(f"Frame {node.frame_seq}: Zero changes detected (no transmission needed).")
            
        if args.once:
            break
        time.sleep(1.0 / max(0.1, args.fps))


if __name__ == "__main__":
    main()
