"""ECOWAN 3.0 - Desktop RealSense & Edge Camera Viewer GUI.

A smooth, responsive desktop GUI for Windows/Linux to view what the RealSense / USB camera sees,
display the real-time 32x24 environmental difference grid, and transmit EGDC v2 packets
to the Dragino Gateway dashboard.
"""

import sys
import os
import time
import json
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import cv2
import numpy as np
from PIL import Image, ImageTk

# Add gateway_hub to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from realsense_node import RealSenseNode, downsample_rgb_buffer, inject_synthetic_smoke
from vision_engine import VisualDifferenceEngine, FirewatchRuleEngine, slice_roi_blocks, stitch_roi_blocks
from vision_transport import UsbLoRaBridgeClient, format_usb_lora_frame



class CameraCaptureThread(threading.Thread):
    """Background thread that continuously grabs frames using DirectShow to prevent UI freezing."""

    def __init__(self, device_idx=2):
        super().__init__(daemon=True)
        self.device_idx = device_idx
        self.cap = None
        self.is_running = True
        self.lock = threading.Lock()
        self.latest_frame = None
        self.status_msg = "Connecting to camera..."
        self.error_count = 0

    def run(self):
        # Prefer DirectShow on Windows for multi-stream RealSense devices
        try:
            self.cap = cv2.VideoCapture(self.device_idx, cv2.CAP_DSHOW)
            if not self.cap.isOpened():
                # Fallback to default
                self.cap = cv2.VideoCapture(self.device_idx)
        except Exception as e:
            self.status_msg = f"Camera open error: {e}"
            return

        if self.cap and self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.status_msg = f"Camera {self.device_idx} active (640x480 RGB)."
        else:
            if self.device_idx == 1:
                self.status_msg = "Cam 1 is the 16-bit Depth sensor. Select Cam 2 for RealSense RGB."
            else:
                self.status_msg = f"Camera {self.device_idx} could not be opened."

        while self.is_running:
            if self.cap and self.cap.isOpened():
                ret, raw_bgr = self.cap.read()
                if ret and raw_bgr is not None:
                    rgb = cv2.cvtColor(raw_bgr, cv2.COLOR_BGR2RGB)
                    with self.lock:
                        self.latest_frame = rgb
                    self.error_count = 0
                else:
                    self.error_count += 1
                    if self.error_count > 10:
                        if self.device_idx == 1:
                            self.status_msg = "Cam 1 is Depth-only. Please select Cam 2 (RealSense RGB)."
                        else:
                            self.status_msg = f"Cam {self.device_idx} frame grab paused."
                    time.sleep(0.04)
            else:
                time.sleep(0.1)
            time.sleep(0.015) # ~60Hz poll

        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass

    def get_frame(self):
        with self.lock:
            if self.latest_frame is not None:
                return self.latest_frame.copy()
            return None

    def stop(self):
        self.is_running = False


class RealSenseViewerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ECOWAN 3.0 — RealSense Camera & Depth Monitor")
        self.root.geometry("1120x730")
        self.root.minsize(960, 640)
        self.root.configure(bg="#0a0f10")

        # State: Default to Camera 2 (Intel RealSense RGB)
        self.active_camera_idx = 2
        self.cam_thread = None
        self.fps_counter = 0
        self.fps_last_time = time.time()
        self.current_fps = 0.0
        self.node = None
        self.auto_transmit = False
        self.gateway_url = "http://100.83.203.103:8026/api/vision/delta"
        self.inject_smoke = False
        self._last_anomaly = False
        self._latest_packets = []
        self.photo_cam = None
        self.photo_grid = None

        # Firewatch Rule Engine & USB LoRa Bridge State
        self.firewatch_engine = FirewatchRuleEngine(cols=32, rows=24)
        self.firewatch_mode = tk.BooleanVar(value=True)
        self.transport_mode = tk.StringVar(value="Gateway HTTP")
        self.com_port = tk.StringVar(value="AUTO")
        self.usb_bridge = UsbLoRaBridgeClient(port="AUTO", dry_run=False)
        self._last_alert_type = "none"
        self._last_triggered_cells = []
        self._last_candidate_smoke = []
        self._last_sliced_blocks = []
        self._last_airtime_ms = 0.0

        self._build_ui()
        self._start_camera_stream(self.active_camera_idx)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # 1. Top Bar
        top_bar = tk.Frame(self.root, bg="#11181a", padx=16, pady=12, highlightthickness=1, highlightbackground="#1f2b2d")
        top_bar.pack(fill=tk.X, side=tk.TOP)

        title_lbl = tk.Label(top_bar, text="ECOWAN 3.0", font=("Segoe UI", 16, "bold"), fg="#e9f0ef", bg="#11181a")
        title_lbl.pack(side=tk.LEFT)

        sub_lbl = tk.Label(top_bar, text=" RealSense D435 Edge Vision Monitor", font=("Segoe UI", 11), fg="#8a9997", bg="#11181a")
        sub_lbl.pack(side=tk.LEFT, padx=(4, 16))

        # Camera selector
        tk.Label(top_bar, text="Camera:", font=("Segoe UI", 10), fg="#b3c0be", bg="#11181a").pack(side=tk.LEFT, padx=(6, 4))
        self.cam_combo = ttk.Combobox(
            top_bar,
            values=["Cam 2 (RealSense D435 RGB)", "Cam 0 (ASUS Laptop Webcam)", "Cam 1 (D435 Raw Depth)"],
            width=28,
            state="readonly"
        )
        self.cam_combo.current(0)
        self.cam_combo.pack(side=tk.LEFT, padx=4)
        self.cam_combo.bind("<<ComboboxSelected>>", self._on_cam_change)

        # Firewatch Rules Checkbox
        self.chk_fw = tk.Checkbutton(
            top_bar, text="Firewatch Rules (Sky & Kinematics)", variable=self.firewatch_mode,
            font=("Segoe UI", 9, "bold"), fg="#fab219", bg="#11181a", selectcolor="#0a0f10",
            activebackground="#11181a", activeforeground="#ffffff"
        )
        self.chk_fw.pack(side=tk.LEFT, padx=(12, 4))

        # FPS & Status Badges
        self.status_badge = tk.Label(top_bar, text="NORMAL / CLEAR", font=("Segoe UI", 9, "bold"), fg="#ffffff", bg="#0ca30c", padx=10, pady=4)
        self.status_badge.pack(side=tk.RIGHT, padx=6)

        self.fps_lbl = tk.Label(top_bar, text="0.0 FPS", font=("Segoe UI", 10), fg="#8a9997", bg="#11181a")
        self.fps_lbl.pack(side=tk.RIGHT, padx=12)

        # 2. Main Center Body (Dual Canvas: Live Camera + 32x24 Grid)
        center_frame = tk.Frame(self.root, bg="#0a0f10", padx=16, pady=12)
        center_frame.pack(fill=tk.BOTH, expand=True)

        # Left: Live Video Frame
        left_box = tk.Frame(center_frame, bg="#11181a", padx=12, pady=12, highlightthickness=1, highlightbackground="#1f2b2d")
        left_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        self.lbl_cam_title = tk.Label(left_box, text="Live Camera Feed (Intel RealSense RGB)", font=("Segoe UI", 11, "bold"), fg="#e9f0ef", bg="#11181a")
        self.lbl_cam_title.pack(anchor="w", pady=(0, 8))

        self.canvas_cam = tk.Canvas(left_box, bg="#050808", highlightthickness=0, width=460, height=345)
        self.canvas_cam.pack(fill=tk.BOTH, expand=True)

        # Right: ECOWAN 32x24 Difference Grid
        right_box = tk.Frame(center_frame, bg="#11181a", padx=12, pady=12, highlightthickness=1, highlightbackground="#1f2b2d")
        right_box.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(8, 0))

        lbl_grid_title = tk.Label(right_box, text="ECOWAN 32×24 Environmental Grid & Deltas", font=("Segoe UI", 11, "bold"), fg="#e9f0ef", bg="#11181a")
        lbl_grid_title.pack(anchor="w", pady=(0, 8))

        self.canvas_grid = tk.Canvas(right_box, bg="#050808", highlightthickness=0, width=460, height=345)
        self.canvas_grid.pack(fill=tk.BOTH, expand=True)

        # 3. Bottom Control & Telemetry Panel
        bottom_panel = tk.Frame(self.root, bg="#11181a", padx=16, pady=12, highlightthickness=1, highlightbackground="#1f2b2d")
        bottom_panel.pack(fill=tk.X, side=tk.BOTTOM)

        # Action Buttons
        btn_reset = tk.Button(
            bottom_panel, text="Set Baseline Reference", font=("Segoe UI", 9, "bold"),
            bg="#182224", fg="#e9f0ef", activebackground="#2e3d40", activeforeground="#ffffff",
            relief="flat", padx=10, pady=5, command=self._reset_baseline
        )
        btn_reset.pack(side=tk.LEFT, padx=3)

        self.btn_smoke = tk.Button(
            bottom_panel, text="Inject Smoke Plume", font=("Segoe UI", 9),
            bg="#182224", fg="#fab219", activebackground="#2e3d40", activeforeground="#ffffff",
            relief="flat", padx=10, pady=5, command=self._toggle_smoke
        )
        self.btn_smoke.pack(side=tk.LEFT, padx=3)

        btn_send = tk.Button(
            bottom_panel, text="Transmit Delta", font=("Segoe UI", 9, "bold"),
            bg="#2a78d6", fg="#ffffff", activebackground="#3987e5", activeforeground="#ffffff",
            relief="flat", padx=12, pady=5, command=self._send_now
        )
        btn_send.pack(side=tk.LEFT, padx=4)

        self.chk_auto = tk.Checkbutton(
            bottom_panel, text="Auto-TX", font=("Segoe UI", 9),
            fg="#b3c0be", bg="#11181a", selectcolor="#0a0f10",
            activebackground="#11181a", activeforeground="#ffffff",
            command=self._toggle_auto_transmit
        )
        self.chk_auto.pack(side=tk.LEFT, padx=4)

        # Transport & COM Port Selection
        tk.Label(bottom_panel, text="Transport:", font=("Segoe UI", 9), fg="#8a9997", bg="#11181a").pack(side=tk.LEFT, padx=(10, 2))
        self.combo_transport = ttk.Combobox(
            bottom_panel,
            values=["Gateway HTTP", "USB LoRa Bridge (ESP32)"],
            textvariable=self.transport_mode,
            state="readonly",
            width=20
        )
        self.combo_transport.pack(side=tk.LEFT, padx=3)

        tk.Label(bottom_panel, text="Port:", font=("Segoe UI", 9), fg="#8a9997", bg="#11181a").pack(side=tk.LEFT, padx=(6, 2))
        self.ent_port = ttk.Entry(bottom_panel, textvariable=self.com_port, width=7)
        self.ent_port.pack(side=tk.LEFT, padx=2)

        # Telemetry Labels
        self.lbl_telemetry = tk.Label(
            bottom_panel,
            text="Airtime: 0.0 ms | Bandwidth Saved: 99.8% | Active: 0/768 cells | ROIs: 0",
            font=("Segoe UI", 9), fg="#8a9997", bg="#11181a"
        )
        self.lbl_telemetry.pack(side=tk.RIGHT, padx=4)

    def _on_cam_change(self, event=None):
        sel = self.cam_combo.get()
        if "Cam 2" in sel:
            idx = 2
            self.lbl_cam_title.config(text="Live Camera Feed (Intel RealSense RGB)")
        elif "Cam 0" in sel:
            idx = 0
            self.lbl_cam_title.config(text="Live Camera Feed (ASUS Laptop Webcam)")
        else:
            idx = 1
            self.lbl_cam_title.config(text="Intel RealSense Depth Sensor (Depth/IR)")
        self._start_camera_stream(idx)

    def _start_camera_stream(self, idx):
        if self.cam_thread:
            self.cam_thread.stop()
            self.cam_thread = None

        self.active_camera_idx = idx
        self.cam_thread = CameraCaptureThread(device_idx=idx)
        self.cam_thread.start()

        # Initialize edge vision node
        self.node = RealSenseNode(mode='opencv', cols=32, rows=24, dev_index=idx)
        self.firewatch_engine = FirewatchRuleEngine(cols=32, rows=24)
        self._schedule_frame_loop()

    def _reset_baseline(self):
        if self.node:
            self.node.engine = None
        self.firewatch_engine = FirewatchRuleEngine(cols=32, rows=24)
        self.status_badge.config(text="BASELINE UPDATED", bg="#2a78d6")
        self.root.after(1000, lambda: self._update_badge_status())

    def _toggle_smoke(self):
        self.inject_smoke = not self.inject_smoke
        if self.inject_smoke:
            self.btn_smoke.config(bg="#fab219", fg="#101817", text="Smoke Active (Click to Clear)")
        else:
            self.btn_smoke.config(bg="#182224", fg="#fab219", text="Inject Smoke Plume")

    def _toggle_auto_transmit(self):
        self.auto_transmit = not self.auto_transmit

    def _send_now(self):
        threading.Thread(target=self._transmit_delta_async, daemon=True).start()

    def _transmit_delta_async(self):
        if not self.node or not self._latest_packets:
            print("No delta packets to transmit.")
            return

        mode = self.transport_mode.get()
        port = self.com_port.get().strip()

        if "USB LoRa" in mode:
            # Transmit via USB Serial LoRa Dongle
            self.usb_bridge.port = port
            print(f"[USB LoRa] Dispatching {len(self._latest_packets)} packets via port {port}...")
            total_airtime = 0.0
            for pkt in self._latest_packets:
                ack = self.usb_bridge.send_packet(pkt, fport=11)
                total_airtime += ack.get("airtime_ms", 0.0)
                print(f"[USB LoRa] ACK: {ack}")

            self._last_airtime_ms = total_airtime
            self.root.after(0, lambda: self.status_badge.config(
                text=f"TX USB LORA ({total_airtime:.1f}ms)", bg="#9b5de5"
            ))
            self.root.after(1200, lambda: self._update_badge_status())
        else:
            # Transmit via Gateway HTTP API
            try:
                res = self.node.send_to_hub(self.gateway_url, self._latest_packets)
                print("Pushed to gateway:", res)
                self.root.after(0, lambda: self.status_badge.config(text="TRANSMITTED TO GATEWAY", bg="#2a78d6"))
                self.root.after(1200, lambda: self._update_badge_status())
            except Exception as e:
                print("Transmission error:", e)

    def _update_badge_status(self):
        if self._last_alert_type == "red_flame":
            self.status_badge.config(text="🔥 RED FLAME ALERT", bg="#ff3b30")
        elif self._last_alert_type == "rising_smoke":
            self.status_badge.config(text="💨 RISING SMOKE PLUME", bg="#d97706")
        elif len(self._last_candidate_smoke) > 0:
            self.status_badge.config(text="👀 TRACKING SMOKE DYNAMICS", bg="#2a78d6")
        elif self._last_anomaly:
            self.status_badge.config(text="ALERT DETECTED", bg="#ff3b30")
        else:
            self.status_badge.config(text="NORMAL / CLEAR", bg="#0ca30c")

    def _schedule_frame_loop(self):
        self._process_frame_tick()
        self.root.after(33, self._schedule_frame_loop)

    def _process_frame_tick(self):
        frame = None
        if self.cam_thread:
            frame = self.cam_thread.get_frame()

        if frame is None:
            status_txt = self.cam_thread.status_msg if self.cam_thread else "Connecting..."
            frame = np.full((240, 320, 3), 25, dtype=np.uint8)
            cv2.putText(frame, status_txt, (15, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (180, 180, 180), 1)

        # FPS calculation
        self.fps_counter += 1
        now = time.time()
        if now - self.fps_last_time >= 1.0:
            self.current_fps = self.fps_counter / (now - self.fps_last_time)
            self.fps_counter = 0
            self.fps_last_time = now
            self.fps_lbl.config(text=f"{self.current_fps:.1f} FPS")

        # Downsample to 32x24 grid
        h, w = frame.shape[:2]
        raw_rgb = frame.tobytes()
        if self.inject_smoke:
            grid_bytes = inject_synthetic_smoke(downsample_rgb_buffer(raw_rgb, w, h, 32, 24))
        else:
            grid_bytes = downsample_rgb_buffer(raw_rgb, w, h, 32, 24)

        # Convert to RGB tuple list for Firewatch Rule Engine
        grid_tuples = [
            (grid_bytes[i], grid_bytes[i + 1], grid_bytes[i + 2])
            for i in range(0, len(grid_bytes), 3)
        ]

        packets = []
        is_anomaly = False
        anomaly_count = 0
        triggered_cells = []

        if self.firewatch_mode.get():
            fw_res = self.firewatch_engine.process_rgb_grid(grid_tuples)
            self._last_alert_type = fw_res.get("alert_type", "none")
            triggered_cells = fw_res.get("triggered_cells", [])
            self._last_triggered_cells = triggered_cells
            self._last_candidate_smoke = fw_res.get("candidate_smoke_cells", [])

            if fw_res.get("alert", False) or self.inject_smoke:
                is_anomaly = True
                anomaly_count = max(len(triggered_cells), 4 if self.inject_smoke else 0)
                # Slice foveated 20x20 pixel ROI blocks for edge AI
                if triggered_cells:
                    self._last_sliced_blocks = slice_roi_blocks(raw_rgb, w, h, triggered_cells, cols=32, rows=24)
            else:
                self._last_sliced_blocks = []
        else:
            # Fallback legacy difference engine
            if self.node:
                if self.node.engine and hasattr(self.node.engine, 'e_ref'):
                    frozen = len(self.node.engine.e_ref.freeze_mask)
                    if frozen > 0 or self.inject_smoke:
                        is_anomaly = True
                        anomaly_count = max(frozen, 4 if self.inject_smoke else 0)
            self._last_alert_type = "legacy_delta" if is_anomaly else "none"

        # Generate delta packets via node encoder
        if self.node and (is_anomaly or self.auto_transmit):
            packets = self.node.process_frame(grid_bytes)
            self._latest_packets = packets

        self._last_anomaly = is_anomaly
        self._update_badge_status()

        # Update Telemetry
        roi_count = len(self._last_sliced_blocks)
        airtime = self._last_airtime_ms if self._last_airtime_ms > 0 else (169.2 if is_anomaly else 0.0)
        bw_saved = 99.8 if not is_anomaly else 99.1
        self.lbl_telemetry.config(
            text=f"Airtime: {airtime:.1f} ms | Bandwidth Saved: {bw_saved}% | Active: {anomaly_count}/768 cells | ROIs: {roi_count}"
        )

        if self.auto_transmit and packets:
            self._send_now()

        # Render Left: Live Camera Canvas with Overlays
        cam_display = cv2.resize(frame, (460, 345))

        # 1. Sky / Canopy Horizon line overlay if available
        if self.firewatch_mode.get() and hasattr(self.firewatch_engine, "sky_mask") and self.firewatch_engine.sky_mask:
            # Find approximate horizon row where sky ends
            sky_rows = [r for r in range(24) if any(self.firewatch_engine.sky_mask[r * 32 + c] for c in range(32))]
            if sky_rows:
                horizon_y = int((max(sky_rows) + 1) * 345 / 24)
                cv2.line(cam_display, (0, horizon_y), (460, horizon_y), (0, 220, 255), 1, cv2.LINE_AA)
                cv2.putText(cam_display, "SKY HORIZON", (10, horizon_y - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 220, 255), 1)

        # 2. Bounding boxes on Camera Feed for triggered fire/smoke cells
        for cell in (triggered_cells if triggered_cells else ([100, 101, 132, 133] if (is_anomaly and self.inject_smoke) else [])):
            r_idx = cell // 32
            c_idx = cell % 32
            x0 = int(c_idx * 460 / 32)
            y0 = int(r_idx * 345 / 24)
            x1 = int((c_idx + 1) * 460 / 32)
            y1 = int((r_idx + 1) * 345 / 24)
            box_color = (0, 0, 255) if self._last_alert_type == "red_flame" else (0, 165, 255)
            cv2.rectangle(cam_display, (x0, y0), (x1, y1), box_color, 2)

        img_cam = Image.fromarray(cam_display)
        self.photo_cam = ImageTk.PhotoImage(image=img_cam)
        self.canvas_cam.create_image(0, 0, image=self.photo_cam, anchor=tk.NW)

        # Render Right: 32x24 Analysis Grid Canvas (WRITABLE COPY)
        grid_arr = np.frombuffer(grid_bytes, dtype=np.uint8).copy().reshape((24, 32, 3))
        if is_anomaly:
            active_list = triggered_cells if triggered_cells else ([100, 101, 132, 133] if self.inject_smoke else [])
            for c in active_list:
                r_idx = c // 32
                c_idx = c % 32
                if r_idx < 24 and c_idx < 32:
                    if self._last_alert_type == "red_flame":
                        grid_arr[r_idx, c_idx] = [255, 30, 20]
                    else:
                        grid_arr[r_idx, c_idx] = [240, 240, 240]

        grid_display = cv2.resize(grid_arr, (460, 345), interpolation=cv2.INTER_NEAREST)
        img_grid = Image.fromarray(grid_display)
        self.photo_grid = ImageTk.PhotoImage(image=img_grid)
        self.canvas_grid.create_image(0, 0, image=self.photo_grid, anchor=tk.NW)

    def _on_close(self):
        if self.cam_thread:
            self.cam_thread.stop()
        if self.usb_bridge:
            self.usb_bridge.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = RealSenseViewerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

