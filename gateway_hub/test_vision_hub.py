import unittest


class VisionHubReconstructorAndInferenceTests(unittest.TestCase):
    """Stages 4 & 5 TDD Contract: Shadow-Staged FrameReconstructor, Recovery Policy, and Inference Uncertainty Hook."""

    def _make_keyframe(self, base_rgb565: int = 0x2104) -> list[dict]:
        return [
            {
                "cell_idx": idx,
                "mode": 0,
                "priority": 0,
                "rgb565": base_rgb565,
                "luma": 64.0,
            }
            for idx in range(768)
        ]

    def test_stage4_complete_frame_commits_and_updates_cells(self):
        import payload_codec
        import vision_hub

        recon = vision_hub.FrameReconstructor()
        recon.load_keyframe(self._make_keyframe(0x1000), keyframe_gen_id=5)

        pkt0 = payload_codec.encode_egdc_packet(
            keyframe_gen_id=5, frame_seq=1, pkt_idx=0, pkt_total=2,
            cells=[{"cell_idx": 10, "mode": 0, "priority": 2, "rgb565": 0xAAAA}],
        )
        pkt1 = payload_codec.encode_egdc_packet(
            keyframe_gen_id=5, frame_seq=1, pkt_idx=1, pkt_total=2,
            cells=[{"cell_idx": 20, "mode": 0, "priority": 2, "rgb565": 0xBBBB}],
        )

        # Packet 0 arrives -> staged_partial, committed_frame[10] still untouched (0x1000)
        r0 = recon.ingest_delta_packet(pkt0, now_ms=100)
        self.assertEqual(r0["status"], "staged_partial")
        self.assertAlmostEqual(r0["completeness_pct"], 50.0)
        self.assertEqual(r0["missing_pkt_indices"], [1])
        self.assertEqual(recon.committed_frame[10]["rgb565"], 0x1000)

        # Packet 1 arrives -> 100% complete_applied, committed_frame updated atomically
        r1 = recon.ingest_delta_packet(pkt1, now_ms=200)
        self.assertEqual(r1["status"], "complete_applied")
        self.assertAlmostEqual(r1["completeness_pct"], 100.0)
        self.assertEqual(r1["missing_pkt_indices"], [])
        self.assertEqual(recon.committed_frame[10]["rgb565"], 0xAAAA)
        self.assertEqual(recon.committed_frame[20]["rgb565"], 0xBBBB)

    def test_stage4_stale_keyframe_and_corrupt_packets_rejected(self):
        import payload_codec
        import vision_hub

        recon = vision_hub.FrameReconstructor()
        recon.load_keyframe(self._make_keyframe(0x1000), keyframe_gen_id=10)

        stale_pkt = payload_codec.encode_egdc_packet(
            keyframe_gen_id=9, frame_seq=1, pkt_idx=0, pkt_total=1,
            cells=[{"cell_idx": 5, "mode": 0, "priority": 1, "rgb565": 0x9999}],
        )
        res_stale = recon.ingest_delta_packet(stale_pkt, now_ms=100)
        self.assertEqual(res_stale["status"], "dropped_stale_keyframe")
        self.assertEqual(recon.committed_frame[5]["rgb565"], 0x1000)

        corrupt_pkt = bytearray(
            payload_codec.encode_egdc_packet(
                keyframe_gen_id=10, frame_seq=1, pkt_idx=0, pkt_total=1,
                cells=[{"cell_idx": 5, "mode": 0, "priority": 1, "rgb565": 0x9999}],
            )
        )
        corrupt_pkt[-1] ^= 0xFF
        res_corrupt = recon.ingest_delta_packet(bytes(corrupt_pkt), now_ms=150)
        self.assertEqual(res_corrupt["status"], "rejected_corrupt")
        self.assertEqual(recon.committed_frame[5]["rgb565"], 0x1000)

    def test_stage4_incomplete_frame_timeout_recovery_and_stale_mask(self):
        import payload_codec
        import vision_hub

        recon = vision_hub.FrameReconstructor()
        recon.load_keyframe(self._make_keyframe(0x1000), keyframe_gen_id=2)

        # Frame seq 1: 1 of 2 packets arrives (50% completeness) -> timeout requests selective retransmit first
        pkt0 = payload_codec.encode_egdc_packet(
            keyframe_gen_id=2, frame_seq=1, pkt_idx=0, pkt_total=2,
            cells=[{"cell_idx": 50, "mode": 2, "priority": 7, "gradient_8x8": [200] * 16, "chroma_anchor": 0xF800}],
        )
        recon.ingest_delta_packet(pkt0, now_ms=100)
        rec1 = recon.finalize_frame_timeout(keyframe_gen_id=2, frame_seq=1, min_completeness_pct=80.0, max_nack_retries=1)
        self.assertEqual(rec1["action"], "request_selective_retransmit")
        self.assertEqual(rec1["missing_pkt_indices"], [1])
        # Committed frame cell 50 was NOT blindly overwritten as a complete frame
        self.assertGreater(len(recon.stale_cell_mask), 0)

        # Frame seq 2: only 1 of 4 packets arrives (25% completeness < 50%) -> requests keyframe refresh
        pkt_low = payload_codec.encode_egdc_packet(
            keyframe_gen_id=2, frame_seq=2, pkt_idx=0, pkt_total=4,
            cells=[{"cell_idx": 60, "mode": 0, "priority": 1, "rgb565": 0x2222}],
        )
        recon.ingest_delta_packet(pkt_low, now_ms=500)
        rec2 = recon.finalize_frame_timeout(keyframe_gen_id=2, frame_seq=2, min_completeness_pct=80.0, max_nack_retries=1)
        self.assertEqual(rec2["action"], "request_keyframe_refresh")

    def test_stage5_inference_hook_detects_smoke_on_complete_frame_and_flags_uncertainty_on_partial(self):
        import payload_codec
        import vision_hub

        recon = vision_hub.FrameReconstructor()
        recon.load_keyframe(self._make_keyframe(0x1000), keyframe_gen_id=4)
        detector = vision_hub.RuleBasedSmokeDetector()

        # 1. Clean complete frame -> status == "ok"
        clean_eval = detector.evaluate_frame(recon, min_completeness_pct=75.0)
        self.assertEqual(clean_eval["status"], "ok")
        self.assertFalse(clean_eval["trigger_keyframe"])

        # 2. Complete frame with Mode 2 smoke plume ROI cells -> status == "anomaly_detected"
        smoke_pkt = payload_codec.encode_egdc_packet(
            keyframe_gen_id=4, frame_seq=1, pkt_idx=0, pkt_total=1,
            cells=[
                {"cell_idx": 100, "mode": 2, "priority": 7, "gradient_8x8": [210] * 16, "chroma_anchor": 0xBDF7},
                {"cell_idx": 101, "mode": 2, "priority": 7, "gradient_8x8": [215] * 16, "chroma_anchor": 0xBDF7},
            ],
        )
        recon.ingest_delta_packet(smoke_pkt, now_ms=200)
        smoke_eval = detector.evaluate_frame(recon, min_completeness_pct=75.0)
        self.assertEqual(smoke_eval["status"], "anomaly_detected")
        self.assertIn(100, smoke_eval["anomaly_cells"])
        self.assertGreaterEqual(smoke_eval["confidence"], 0.7)

        # 3. Incomplete frame (1 of 3 packets = 33.3% < 75.0%) -> forces "uncertain_needs_keyframe"
        partial_pkt = payload_codec.encode_egdc_packet(
            keyframe_gen_id=4, frame_seq=2, pkt_idx=0, pkt_total=3,
            cells=[{"cell_idx": 102, "mode": 2, "priority": 7, "gradient_8x8": [220] * 16, "chroma_anchor": 0xBDF7}],
        )
        recon.ingest_delta_packet(partial_pkt, now_ms=400)
        recon.finalize_frame_timeout(keyframe_gen_id=4, frame_seq=2, min_completeness_pct=75.0, max_nack_retries=0)
        uncertain_eval = detector.evaluate_frame(recon, min_completeness_pct=75.0)
        self.assertEqual(uncertain_eval["status"], "uncertain_needs_keyframe")
        self.assertTrue(uncertain_eval["trigger_keyframe"])


if __name__ == "__main__":
    unittest.main()
