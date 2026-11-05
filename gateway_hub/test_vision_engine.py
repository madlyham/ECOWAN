import unittest


class VisionEngineTests(unittest.TestCase):
    """Stage 2 TDD Contract: 4-Stage Visual-Difference Engine, Masked E_ref, and 5-Scenario Benchmark."""

    def _make_blank_grid(self, base_luma: float = 100.0, rgb565: int = 0x4208) -> list[dict]:
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

    def test_deadband_suppresses_sensor_noise_and_leaf_jitter(self):
        import vision_engine
        base = self._make_blank_grid(100.0)
        eng = vision_engine.VisualDifferenceEngine(keyframe_cells=base, keyframe_gen_id=1, t_noise=8.0)

        # Jitter all cells within +/- 5.0 luma (below t_noise = 8.0)
        noisy = self._make_blank_grid(100.0)
        for idx, c in enumerate(noisy):
            c["luma"] = 100.0 + (4.5 if idx % 2 == 0 else -4.5)

        res = eng.process_frame(noisy, frame_seq=1, profile="balanced")
        self.assertEqual(res["changed_cells"], [])
        self.assertEqual(res["packets"], [])
        self.assertEqual(res["cutoff"]["action"], "lora_delta")

    def test_global_exposure_compensation_absorbs_cloud_shadow_shift(self):
        import vision_engine
        base = self._make_blank_grid(120.0)
        eng = vision_engine.VisualDifferenceEngine(keyframe_cells=base, keyframe_gen_id=1, t_noise=8.0)

        # Uniform -25.0 luma shift across all 768 cells (cloud shadow sweep)
        shaded = self._make_blank_grid(95.0)
        res = eng.process_frame(shaded, frame_seq=2, profile="balanced")

        # Global shift is detected (~ -25.0) and compensated so it does not trigger a keyframe flood
        self.assertAlmostEqual(res["global_luma_shift"], -25.0, delta=3.0)
        self.assertEqual(res["cutoff"]["action"], "lora_delta")
        self.assertLessEqual(len(res["packets"]), 2)

    def test_masked_e_ref_freezes_candidate_smoke_cells_against_background_absorption(self):
        import vision_engine
        base = self._make_blank_grid(100.0)
        eng = vision_engine.VisualDifferenceEngine(
            keyframe_cells=base,
            keyframe_gen_id=1,
            t_noise=8.0,
            e_ref_alpha=0.10,
        )

        smoke_cells = {100, 101, 132, 133}
        normal_shifted_cell = 500

        # Over 10 consecutive frames:
        # - Smoke cells have high spatial_variance and persistent +45 luma plume
        # - Normal cell 500 has a gentle low-variance +12 luma drift (e.g. slow sun angle change)
        for seq in range(1, 11):
            frame = self._make_blank_grid(100.0)
            frame[normal_shifted_cell]["luma"] = 112.0
            frame[normal_shifted_cell]["spatial_variance"] = 1.5
            for s_idx in smoke_cells:
                frame[s_idx]["luma"] = 145.0 + seq
                frame[s_idx]["spatial_variance"] = 28.0
                frame[s_idx]["gradient_8x8"] = [140 + (i % 8) * 5 for i in range(16)]
            res = eng.process_frame(frame, frame_seq=seq, profile="balanced")

        # 1. Smoke cells MUST be frozen in E_ref so their baseline remains at 100.0 (not absorbed!)
        for s_idx in smoke_cells:
            self.assertIn(s_idx, eng.e_ref.freeze_mask)
            self.assertAlmostEqual(eng.e_ref.e_ref_luma[s_idx], 100.0, places=2)

        # 2. Normal non-suspicious cell 500 updated its E_ref toward 112.0
        self.assertNotIn(normal_shifted_cell, eng.e_ref.freeze_mask)
        self.assertGreater(eng.e_ref.e_ref_luma[normal_shifted_cell], 103.0)

        # 3. Smoke cells are encoded as Mode 2 (High Priority ROI)
        mode2_indices = {c["cell_idx"] for c in res["changed_cells"] if c["mode"] == 2}
        self.assertTrue(smoke_cells.issubset(mode2_indices))

    def test_keyframe_drift_audit_flags_excessive_reference_drift(self):
        import vision_engine
        base = self._make_blank_grid(80.0)
        eref = vision_engine.EnvironmentalReference(keyframe_cells=base, keyframe_gen_id=1)

        # Force normal update with large alpha to exceed max_drift_from_keyframe (35.0)
        drifted = self._make_blank_grid(80.0)
        drifted[200]["luma"] = 150.0
        audit = eref.update(drifted, candidate_anomaly_indices=set(), alpha=0.8, max_drift_from_keyframe=35.0)
        self.assertIn(200, audit["drift_alert_cells"])

    def test_five_outdoor_scenarios_benchmark_contract(self):
        import vision_engine
        report = vision_engine.run_scenario_benchmark(profile="balanced")
        expected_scenarios = (
            "calm_foliage_wind",
            "cloud_shadow_sweep",
            "emerging_smoke_plume",
            "sudden_intrusion",
            "major_scene_corrupt",
        )
        for name in expected_scenarios:
            self.assertIn(name, report["scenarios"])
            sc = report["scenarios"][name]
            self.assertIn("packets_sent", sc)
            self.assertIn("total_app_bytes", sc)
            self.assertIn("total_phy_bytes", sc)
            self.assertIn("total_airtime_ms", sc)
            self.assertIn("cutoff_action", sc)
            self.assertIn("frozen_e_ref_cells", sc)

        # 1. Calm wind: 0 or <= 1 packet, lora_delta
        self.assertLessEqual(report["scenarios"]["calm_foliage_wind"]["packets_sent"], 1)
        self.assertEqual(report["scenarios"]["calm_foliage_wind"]["cutoff_action"], "lora_delta")

        # 2. Cloud shadow sweep: global illumination compensated, stays on lora_delta
        self.assertEqual(report["scenarios"]["cloud_shadow_sweep"]["cutoff_action"], "lora_delta")

        # 3. Emerging smoke plume: freezes E_ref on smoke ROI and emits Mode 2 patches via lora_delta
        self.assertGreater(report["scenarios"]["emerging_smoke_plume"]["frozen_e_ref_cells"], 0)
        self.assertGreater(report["scenarios"]["emerging_smoke_plume"]["mode2_roi_cells"], 0)
        self.assertEqual(report["scenarios"]["emerging_smoke_plume"]["cutoff_action"], "lora_delta")

        # 4. Sudden intrusion: localized delta sent over LoRa
        self.assertGreater(report["scenarios"]["sudden_intrusion"]["packets_sent"], 0)
        self.assertEqual(report["scenarios"]["sudden_intrusion"]["cutoff_action"], "lora_delta")

        # 5. Major scene corruption: exceeds cutoff and triggers keyframe refresh
        self.assertEqual(report["scenarios"]["major_scene_corrupt"]["cutoff_action"], "keyframe")


if __name__ == "__main__":
    unittest.main()
