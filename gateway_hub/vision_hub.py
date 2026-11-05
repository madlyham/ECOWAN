#!/usr/bin/env python3
"""Stages 4 & 5: Shadow-Staged FrameReconstructor, Loss Recovery, and Inference Uncertainty Hook."""
from __future__ import annotations

import payload_codec


class FrameReconstructor:
    """Receiver-side image reconstructor with shadow staging and recovery policies."""

    def __init__(self):
        self.committed_frame: list[dict] = []
        self.keyframe_gen_id: int = 0
        self.stale_cell_mask: set[int] = set()
        self.shadow_staging: dict[tuple[int, int], dict] = {}
        self.last_applied_cells: list[dict] = []
        self.last_completeness_pct: float = 100.0

    def load_keyframe(self, keyframe_cells: list[dict], keyframe_gen_id: int) -> None:
        self.committed_frame = [dict(c) for c in keyframe_cells]
        self.keyframe_gen_id = keyframe_gen_id
        self.stale_cell_mask.clear()
        self.shadow_staging.clear()
        self.last_applied_cells.clear()
        self.last_completeness_pct = 100.0

    def ingest_delta_packet(self, packet: bytes, now_ms: int = 0) -> dict:
        decoded, err = payload_codec.decode_egdc_packet(packet)
        if err:
            return {"status": "rejected_corrupt", "reason": err}

        pkt_gen_id = decoded["keyframe_gen_id"]
        if pkt_gen_id < self.keyframe_gen_id:
            return {"status": "dropped_stale_keyframe", "reason": f"keyframe_gen_id {pkt_gen_id} < {self.keyframe_gen_id}"}
        if pkt_gen_id > self.keyframe_gen_id:
            return {"status": "dropped_stale_keyframe", "reason": f"unloaded keyframe_gen_id {pkt_gen_id}"}

        frame_seq = decoded["frame_seq"]
        stage_key = (pkt_gen_id, frame_seq)
        stage = self.shadow_staging.setdefault(stage_key, {
            "pkt_total": decoded["pkt_total"],
            "packets": {},
        })
        stage["packets"][decoded["pkt_idx"]] = decoded

        pkt_total = stage["pkt_total"]
        missing_indices = [i for i in range(pkt_total) if i not in stage["packets"]]
        completeness_pct = (len(stage["packets"]) / pkt_total) * 100.0
        self.last_completeness_pct = completeness_pct

        if missing_indices:
            return {
                "status": "staged_partial",
                "completeness_pct": completeness_pct,
                "missing_pkt_indices": missing_indices,
            }

        # Complete frame arrived -> atomically commit into committed_frame
        all_cells = []
        for p_idx in range(pkt_total):
            p = stage["packets"][p_idx]
            for c in p["cells"]:
                idx = c["cell_idx"]
                all_cells.append(c)
                if 0 <= idx < len(self.committed_frame):
                    self.committed_frame[idx].update(c)
                    self.stale_cell_mask.discard(idx)

        self.last_applied_cells = all_cells
        self.last_completeness_pct = 100.0
        del self.shadow_staging[stage_key]

        return {
            "status": "complete_applied",
            "completeness_pct": 100.0,
            "missing_pkt_indices": [],
        }

    def finalize_frame_timeout(
        self,
        keyframe_gen_id: int,
        frame_seq: int,
        min_completeness_pct: float = 80.0,
        max_nack_retries: int = 1,
    ) -> dict:
        stage_key = (keyframe_gen_id, frame_seq)
        stage = self.shadow_staging.get(stage_key)
        if not stage:
            return {"action": "request_keyframe_refresh", "missing_pkt_indices": []}

        pkt_total = stage["pkt_total"]
        missing = [i for i in range(pkt_total) if i not in stage["packets"]]
        completeness_pct = (len(stage["packets"]) / pkt_total) * 100.0
        self.last_completeness_pct = completeness_pct

        # Cells received in incomplete frames are added to stale_cell_mask
        for p in stage["packets"].values():
            for c in p["cells"]:
                self.stale_cell_mask.add(c["cell_idx"])

        if completeness_pct >= 50.0 and max_nack_retries > 0:
            action = "request_selective_retransmit"
        else:
            action = "request_keyframe_refresh"

        del self.shadow_staging[stage_key]
        return {
            "action": action,
            "missing_pkt_indices": missing,
            "completeness_pct": completeness_pct,
        }


class RuleBasedSmokeDetector:
    """Inference hook evaluating smoke anomaly ROIs with incomplete-frame uncertainty gating."""

    def evaluate_frame(self, reconstructor: FrameReconstructor, min_completeness_pct: float = 75.0) -> dict:
        if reconstructor.last_completeness_pct < min_completeness_pct:
            return {
                "status": "uncertain_needs_keyframe",
                "trigger_keyframe": True,
                "confidence": 0.0,
                "anomaly_cells": [],
            }

        anomaly_cells = [
            c["cell_idx"] for c in getattr(reconstructor, "last_applied_cells", [])
            if c.get("mode") == 2
        ]

        if anomaly_cells:
            return {
                "status": "anomaly_detected",
                "anomaly_cells": anomaly_cells,
                "confidence": 0.88,
                "trigger_keyframe": False,
            }

        return {
            "status": "ok",
            "anomaly_cells": [],
            "confidence": 1.0,
            "trigger_keyframe": False,
        }
