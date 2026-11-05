#!/usr/bin/env python3
"""Stage 2: Visual-Difference Engine, Masked E_ref Environmental Reference, and 5-Scenario Benchmark."""
from __future__ import annotations

import statistics
import payload_codec


class EnvironmentalReference:
    """Adaptive environmental reference (E_ref) with anomaly masking and keyframe drift auditing."""

    def __init__(self, keyframe_cells: list[dict], keyframe_gen_id: int = 1):
        self.keyframe_gen_id = keyframe_gen_id
        self.keyframe_luma = [float(c.get("luma", 100.0)) for c in keyframe_cells]
        self.e_ref_luma = list(self.keyframe_luma)
        self.freeze_mask: set[int] = set()

    def update(
        self,
        current_cells: list[dict],
        candidate_anomaly_indices: set[int] | None = None,
        alpha: float = 0.05,
        max_drift_from_keyframe: float = 35.0,
    ) -> dict:
        """Update reference with exponential moving average, freezing candidate anomaly cells."""
        if candidate_anomaly_indices is not None:
            self.freeze_mask = set(candidate_anomaly_indices)

        for idx, cell in enumerate(current_cells):
            if idx not in self.freeze_mask:
                val = float(cell.get("luma", 100.0))
                self.e_ref_luma[idx] = (1.0 - alpha) * self.e_ref_luma[idx] + alpha * val

        drift_alert_cells = set()
        for idx in range(len(self.e_ref_luma)):
            if abs(self.e_ref_luma[idx] - self.keyframe_luma[idx]) > max_drift_from_keyframe:
                drift_alert_cells.add(idx)

        return {
            "drift_alert_cells": drift_alert_cells,
            "frozen_count": len(self.freeze_mask),
        }


class VisualDifferenceEngine:
    """4-stage delta pipeline: exposure normalization, deadband, priority classification, and packing."""

    def __init__(
        self,
        keyframe_cells: list[dict],
        keyframe_gen_id: int = 1,
        t_noise: float = 8.0,
        e_ref_alpha: float = 0.02,
    ):
        self.keyframe_cells = keyframe_cells
        self.keyframe_gen_id = keyframe_gen_id
        self.t_noise = t_noise
        self.e_ref_alpha = e_ref_alpha
        self.e_ref = EnvironmentalReference(keyframe_cells, keyframe_gen_id)

    def process_frame(
        self,
        current_cells: list[dict],
        frame_seq: int,
        profile: str = "balanced",
        max_app_bytes: int = 222,
    ) -> dict:
        raw_diffs = [
            float(c.get("luma", 100.0)) - self.e_ref.e_ref_luma[i]
            for i, c in enumerate(current_cells)
        ]
        # Global illumination shift computed via median to ignore local anomalies
        global_luma_shift = float(statistics.median(raw_diffs))

        changed_cells = []
        candidate_anomalies = set()

        for idx, cell in enumerate(current_cells):
            luma = float(cell.get("luma", 100.0))
            ref_val = self.e_ref.e_ref_luma[idx]
            eff_diff = abs(luma - (ref_val + global_luma_shift))

            if eff_diff > self.t_noise:
                var = float(cell.get("spatial_variance", 2.0))
                # Candidate smoke/fire anomaly: high spatial variance or massive delta
                if var >= 15.0 or eff_diff >= 30.0:
                    mode = 2
                    priority = 7
                    candidate_anomalies.add(idx)
                    changed_cells.append({
                        "cell_idx": idx,
                        "mode": mode,
                        "priority": priority,
                        "gradient_8x8": cell.get("gradient_8x8", [int(luma)] * 16),
                        "chroma_anchor": int(cell.get("chroma_anchor", cell.get("rgb565", 0x4208))),
                    })
                elif var > 5.0 or eff_diff > 15.0:
                    mode = 1
                    priority = 4
                    changed_cells.append({
                        "cell_idx": idx,
                        "mode": mode,
                        "priority": priority,
                        "luma_4x4": cell.get("luma_4x4", [int(luma // 16)] * 16),
                    })
                else:
                    mode = 0
                    priority = 1
                    changed_cells.append({
                        "cell_idx": idx,
                        "mode": mode,
                        "priority": priority,
                        "rgb565": int(cell.get("rgb565", 0x4208)),
                    })

        # Update reference while protecting candidate anomalies from absorption
        self.e_ref.update(
            current_cells,
            candidate_anomaly_indices=candidate_anomalies,
            alpha=self.e_ref_alpha,
        )

        if changed_cells:
            packets = payload_codec.pack_egdc_frames(
                keyframe_gen_id=self.keyframe_gen_id,
                frame_seq=frame_seq,
                cells=changed_cells,
                max_app_bytes=max_app_bytes,
            )
        else:
            packets = []

        cutoff = payload_codec.evaluate_cutoff_policy(
            packets_required=len(packets),
            profile=profile,
        )

        return {
            "frame_seq": frame_seq,
            "keyframe_gen_id": self.keyframe_gen_id,
            "global_luma_shift": global_luma_shift,
            "changed_cells": changed_cells,
            "packets": packets,
            "cutoff": cutoff,
            "frozen_cells_count": len(self.e_ref.freeze_mask),
        }


def _make_grid(base_luma: float = 100.0, rgb565: int = 0x4208) -> list[dict]:
    return [
        {
            "cell_idx": idx,
            "luma": float(base_luma),
            "rgb565": rgb565,
            "luma_4x4": [int(base_luma // 16)] * 16,
            "gradient_8x8": [int(base_luma)] * 16,
            "chroma_anchor": rgb565,
            "spatial_variance": 2.0,
        }
        for idx in range(768)
    ]


def run_scenario_benchmark(profile: str = "balanced") -> dict:
    """Benchmark the 5 standardized outdoor camera scenarios."""
    scenarios = {}

    # 1. calm_foliage_wind
    base1 = _make_grid(100.0)
    eng1 = VisualDifferenceEngine(base1, keyframe_gen_id=1, t_noise=8.0)
    wind_frame = _make_grid(100.0)
    for i in range(768):
        wind_frame[i]["luma"] = 100.0 + (3.5 if i % 2 == 0 else -3.5)
    r1 = eng1.process_frame(wind_frame, frame_seq=1, profile=profile)
    scenarios["calm_foliage_wind"] = _summarize_result(r1)

    # 2. cloud_shadow_sweep
    base2 = _make_grid(120.0)
    eng2 = VisualDifferenceEngine(base2, keyframe_gen_id=1, t_noise=8.0)
    cloud_frame = _make_grid(95.0)
    r2 = eng2.process_frame(cloud_frame, frame_seq=1, profile=profile)
    scenarios["cloud_shadow_sweep"] = _summarize_result(r2)

    # 3. emerging_smoke_plume
    base3 = _make_grid(100.0)
    eng3 = VisualDifferenceEngine(base3, keyframe_gen_id=1, t_noise=8.0)
    smoke_frame = _make_grid(100.0)
    smoke_cells = {100, 101, 132, 133}
    for s_idx in smoke_cells:
        smoke_frame[s_idx]["luma"] = 150.0
        smoke_frame[s_idx]["spatial_variance"] = 28.0
        smoke_frame[s_idx]["gradient_8x8"] = [140 + (i % 8) * 5 for i in range(16)]
    r3 = eng3.process_frame(smoke_frame, frame_seq=1, profile=profile)
    s3_summary = _summarize_result(r3)
    s3_summary["mode2_roi_cells"] = sum(1 for c in r3["changed_cells"] if c["mode"] == 2)
    scenarios["emerging_smoke_plume"] = s3_summary

    # 4. sudden_intrusion
    base4 = _make_grid(100.0)
    eng4 = VisualDifferenceEngine(base4, keyframe_gen_id=1, t_noise=8.0)
    intrus_frame = _make_grid(100.0)
    for i in range(10, 16):
        intrus_frame[i]["luma"] = 125.0
        intrus_frame[i]["spatial_variance"] = 4.0
    r4 = eng4.process_frame(intrus_frame, frame_seq=1, profile=profile)
    scenarios["sudden_intrusion"] = _summarize_result(r4)

    # 5. major_scene_corrupt
    base5 = _make_grid(100.0)
    eng5 = VisualDifferenceEngine(base5, keyframe_gen_id=1, t_noise=8.0)
    corrupt_frame = _make_grid(100.0)
    for i in range(400):
        corrupt_frame[i]["luma"] = 180.0
        corrupt_frame[i]["spatial_variance"] = 25.0
    r5 = eng5.process_frame(corrupt_frame, frame_seq=1, profile=profile, max_app_bytes=50)
    scenarios["major_scene_corrupt"] = _summarize_result(r5)

    return {"scenarios": scenarios}


def _summarize_result(res: dict) -> dict:
    pkts = res.get("packets", [])
    total_app = sum(len(p) for p in pkts)
    total_phy = total_app + len(pkts) * 13
    total_airtime = 0.0
    for p in pkts:
        air = payload_codec.calculate_lora_airtime(app_bytes=len(p), sf=7, bw_khz=125, mac_overhead_bytes=13)
        total_airtime += air["airtime_ms"]

    return {
        "packets_sent": len(pkts),
        "total_app_bytes": total_app,
        "total_phy_bytes": total_phy,
        "total_airtime_ms": total_airtime,
        "cutoff_action": res["cutoff"]["action"],
        "frozen_e_ref_cells": res.get("frozen_cells_count", 0),
    }


class FirewatchRuleEngine:
    """Specialized firewatch tower rule engine prioritizing Red (flame), Grey/Black (smoke)
    gated by Sky context and upward-rising kinematics (rejecting ground shadows and horizontal cloud sweeps).
    """

    def __init__(self, cols: int = 32, rows: int = 24):
        self.cols = cols
        self.rows = rows
        self.sky_mask: list[bool] = [False] * (cols * rows)
        self.ref_scene: list[tuple[int, int, int]] | None = None
        self.history: list[set[int]] = []

    def build_sky_mask(self, scene: list[tuple[int, int, int]]) -> list[bool]:
        """Classify each column's upper contiguous blue/bright cells as sky."""
        mask = [False] * (self.cols * self.rows)
        for c in range(self.cols):
            in_sky = True
            for r in range(self.rows):
                idx = r * self.cols + c
                rgb = scene[idx]
                red, green, blue = rgb
                is_canopy = (green > red and green > blue and green > 60 and blue < 150)
                if in_sky and not is_canopy:
                    mask[idx] = True
                else:
                    in_sky = False
                    mask[idx] = False
        self.sky_mask = mask
        return mask

    def classify_color(self, rgb: tuple[int, int, int], ref_rgb: tuple[int, int, int] | None = None) -> str:
        """Classify RGB pixel as 'red', 'grey', 'black', or 'normal'."""
        r, g, b = rgb
        luma = 0.299 * r + 0.587 * g + 0.114 * b
        ref_luma = (0.299 * ref_rgb[0] + 0.587 * ref_rgb[1] + 0.114 * ref_rgb[2]) if ref_rgb else luma

        # 1. Active flame core: high red dominance
        if r > 140 and r > 1.3 * g and r > 1.5 * b:
            return "red"

        luma_diff = abs(luma - ref_luma)
        saturation = max(r, g, b) - min(r, g, b)

        # 2. Dense carbon smoke: dark, desaturated
        if saturation <= 15 and luma < 75 and (luma_diff > 12 or (ref_rgb and (ref_rgb[2] > 180 or ref_rgb[1] > 70))):
            return "black"

        # 3. Ash / steam plume: medium-bright, desaturated, significant shift from saturated ref
        if saturation <= 18 and luma >= 75 and (luma_diff > 10 or (ref_rgb and (ref_rgb[2] > 180 or ref_rgb[1] > 70))):
            return "grey"

        return "normal"

    def initialize_reference(self, scene: list[tuple[int, int, int]]) -> None:
        self.ref_scene = list(scene)
        self.build_sky_mask(scene)
        self.history.clear()

    def has_sky_context(self, cell_idx: int) -> bool:
        """True if the cell is sky or directly borders a sky cell (within 1 row/col of sky)."""
        if self.sky_mask[cell_idx]:
            return True
        r = cell_idx // self.cols
        c = cell_idx % self.cols
        for dr in (-1, 0, 1):
            nr = r + dr
            if 0 <= nr < self.rows:
                for dc in (-1, 0, 1):
                    nc = c + dc
                    if 0 <= nc < self.cols:
                        if self.sky_mask[nr * self.cols + nc]:
                            return True
        return False

    def process_rgb_grid(self, frame: list[tuple[int, int, int]]) -> dict:
        """Process one frame against reference and history with sky gating and upward rising kinematics."""
        if self.ref_scene is None:
            self.initialize_reference(frame)
            return {"alert": False, "alert_type": "none", "triggered_cells": []}

        red_cells = []
        candidate_smoke_cells = set()

        for idx, rgb in enumerate(frame):
            ref_c = self.ref_scene[idx]
            color_class = self.classify_color(rgb, ref_c)
            if color_class == "red":
                red_cells.append(idx)
            elif color_class in ("grey", "black"):
                if self.has_sky_context(idx):
                    candidate_smoke_cells.add(idx)

        # Red flame triggers immediately
        if red_cells:
            return {
                "alert": True,
                "alert_type": "red_flame",
                "triggered_cells": red_cells,
                "candidate_smoke_cells": list(candidate_smoke_cells),
            }

        # Grey/Black smoke kinematic rising check
        self.history.append(candidate_smoke_cells)
        if len(self.history) > 5:
            self.history.pop(0)

        # Check upward propagation across frames
        if len(self.history) >= 2 and candidate_smoke_cells:
            prev_cells = self.history[-2]
            if prev_cells:
                min_curr_row = min(idx // self.cols for idx in candidate_smoke_cells)
                min_prev_row = min(idx // self.cols for idx in prev_cells)
                if min_curr_row < min_prev_row:
                    triggered = sorted(list(candidate_smoke_cells))
                    return {
                        "alert": True,
                        "alert_type": "rising_smoke",
                        "triggered_cells": triggered,
                        "candidate_smoke_cells": list(candidate_smoke_cells),
                    }

        return {
            "alert": False,
            "alert_type": "none",
            "triggered_cells": [],
            "candidate_smoke_cells": list(candidate_smoke_cells),
        }


def slice_roi_blocks(
    live_bytes: bytes,
    width: int,
    height: int,
    cell_indices: list[int],
    cols: int = 32,
    rows: int = 24,
) -> list[dict]:
    """Slice native pixel blocks from full-resolution RGB frame for triggered cells."""
    bw = width // cols
    bh = height // rows
    blocks = []

    for cell in cell_indices:
        r = cell // cols
        c = cell % cols
        x0 = c * bw
        y0 = r * bh

        patch = bytearray(bw * bh * 3)
        patch_idx = 0
        for y in range(y0, y0 + bh):
            row_start = y * width * 3
            col_start = row_start + x0 * 3
            col_end = col_start + bw * 3
            patch[patch_idx : patch_idx + (bw * 3)] = live_bytes[col_start:col_end]
            patch_idx += bw * 3

        blocks.append({
            "cell_idx": cell,
            "x": x0,
            "y": y0,
            "w": bw,
            "h": bh,
            "rgb_bytes": bytes(patch),
        })

    return blocks


def stitch_roi_blocks(
    ref_bytes: bytes,
    width: int,
    height: int,
    blocks: list[dict],
    cols: int = 32,
    rows: int = 24,
) -> bytes:
    """Stitch received high-resolution ROI pixel blocks onto the reference background canvas."""
    canvas = bytearray(ref_bytes)
    for block in blocks:
        x0 = block["x"]
        y0 = block["y"]
        bw = block["w"]
        bh = block["h"]
        patch = block["rgb_bytes"]

        patch_idx = 0
        for y in range(y0, y0 + bh):
            row_start = y * width * 3
            col_start = row_start + x0 * 3
            col_end = col_start + bw * 3
            canvas[col_start:col_end] = patch[patch_idx : patch_idx + (bw * 3)]
            patch_idx += bw * 3

    return bytes(canvas)

