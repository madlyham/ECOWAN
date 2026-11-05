import unittest
from pathlib import Path


class VisionDashboardApiTests(unittest.TestCase):
    """Stage 6 TDD Contract: Edge Monitor Vision Lab REST Handlers and Dashboard UI Hooks."""

    def test_vision_status_simulate_and_delta_ingest_handlers(self):
        import edge_dash
        import payload_codec

        # 1. Initial vision status check
        status = edge_dash.handle_vision_status()
        self.assertIn("keyframe_gen_id", status)
        self.assertIn("frame_seq", status)
        self.assertIn("completeness_pct", status)
        self.assertIn("stale_cell_count", status)
        self.assertIn("inference", status)
        self.assertIn("cutoff_profiles", status)

        # 2. Run synthetic scenario simulation via API handler ("emerging_smoke_plume")
        sim_res = edge_dash.handle_vision_simulate({
            "scenario": "emerging_smoke_plume",
            "profile": "balanced",
            "loss_rate": 0.0,
        })
        self.assertEqual(sim_res["scenario"], "emerging_smoke_plume")
        self.assertGreater(sim_res["packets_sent"], 0)
        self.assertGreater(sim_res["frozen_e_ref_cells"], 0)
        self.assertEqual(sim_res["inference"]["status"], "anomaly_detected")

        # 3. Direct hex delta ingest via API handler
        gen_id = sim_res["keyframe_gen_id"]
        pkt = payload_codec.encode_egdc_packet(
            keyframe_gen_id=gen_id,
            frame_seq=99,
            pkt_idx=0,
            pkt_total=1,
            cells=[{"cell_idx": 12, "mode": 0, "priority": 2, "rgb565": 0x07E0}],
        )
        ingest_res = edge_dash.handle_vision_ingest_delta({"hex": pkt.hex()})
        self.assertEqual(ingest_res["status"], "complete_applied")
        self.assertAlmostEqual(ingest_res["completeness_pct"], 100.0)

    def test_index_html_contains_vision_lab_ui_and_preserves_existing_views(self):
        html_path = Path(__file__).with_name("index.html")
        html = html_path.read_text(encoding="utf-8")

        # Vision Lab UI hooks required for Stage 6
        self.assertTrue("vision-lab" in html, "Missing 'vision-lab' UI container in index.html")
        self.assertTrue("/api/vision/status" in html, "Missing '/api/vision/status' endpoint hook in index.html")
        self.assertTrue("/api/vision/simulate" in html, "Missing '/api/vision/simulate' endpoint hook in index.html")

        # Existing dashboard & codegen regression hooks must remain intact
        self.assertTrue("ECOWAN" in html, "Missing 'ECOWAN' branding in index.html")
        self.assertTrue("generateSingleFileSketch" in html, "Missing 'generateSingleFileSketch' in index.html")
        self.assertTrue("api/nodes" in html, "Missing 'api/nodes' in index.html")


if __name__ == "__main__":
    unittest.main()
