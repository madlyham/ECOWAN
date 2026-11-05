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
