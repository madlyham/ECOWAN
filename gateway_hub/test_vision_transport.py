import unittest


class VisionTransportAndSchedulerTests(unittest.TestCase):
    """Stage 3 TDD Contract: DeltaTransport Abstraction and MultiRadioKeyframeScheduler."""

    def test_simulated_lora_transport_delivery_drop_and_corruption(self):
        import payload_codec
        import vision_transport

        pkt0 = payload_codec.encode_egdc_packet(1, 1, 0, 3, [{"cell_idx": 1, "mode": 0, "priority": 1, "rgb565": 0x1111}])
        pkt1 = payload_codec.encode_egdc_packet(1, 1, 1, 3, [{"cell_idx": 2, "mode": 0, "priority": 1, "rgb565": 0x2222}])
        pkt2 = payload_codec.encode_egdc_packet(1, 1, 2, 3, [{"cell_idx": 3, "mode": 0, "priority": 1, "rgb565": 0x3333}])

        # Drop packet index 1, corrupt packet index 2
        sim = vision_transport.SimulatedLoRaTransport(
            drop_packet_indices={1},
            corrupt_packet_indices={2},
        )
        r0 = sim.send_packet(pkt0, channel_idx=0, dr=5, now_ms=100)
        r1 = sim.send_packet(pkt1, channel_idx=1, dr=5, now_ms=100)
        r2 = sim.send_packet(pkt2, channel_idx=0, dr=5, now_ms=500)

        self.assertTrue(r0["sent"])
        self.assertFalse(r1["sent"])
        self.assertTrue(r2["sent"])

        received = sim.receive_packets()
        self.assertEqual(len(received), 2)
        # First received packet is intact pkt0
        d0, err0 = payload_codec.decode_egdc_packet(received[0])
        self.assertIsNone(err0)
        self.assertEqual(d0["pkt_idx"], 0)
        # Second received packet was corrupted in transit -> fails CRC-8
        _, err2 = payload_codec.decode_egdc_packet(received[1])
        self.assertIsNotNone(err2)
        self.assertIn("crc", err2.lower())

    def test_serial_lora_transport_dry_run_stub_without_hardware(self):
        import payload_codec
        import vision_transport

        pkt = payload_codec.encode_egdc_packet(1, 1, 0, 1, [{"cell_idx": 5, "mode": 0, "priority": 2, "rgb565": 0x0F0F}])
        serial_stub = vision_transport.SerialLoRaTransport(port="COM99", baudrate=115200, dry_run=True)
        res = serial_stub.send_packet(pkt, channel_idx=0, dr=5, now_ms=1000)
        self.assertTrue(res["sent"])
        self.assertTrue(res["dry_run"])
        self.assertIn(pkt.hex().upper(), res["wire_frame"].upper())

    def test_scheduler_priority_preemption_and_multi_radio_dispatch(self):
        import payload_codec
        import vision_transport

        low_pkt = payload_codec.encode_egdc_packet(1, 10, 0, 2, [{"cell_idx": 10, "mode": 0, "priority": 1, "rgb565": 0x0001}])
        high_pkt = payload_codec.encode_egdc_packet(
            1, 10, 1, 2,
            [{"cell_idx": 20, "mode": 2, "priority": 7, "gradient_8x8": [5] * 16, "chroma_anchor": 0x1234}],
        )

        sched = vision_transport.MultiRadioKeyframeScheduler(
            camera_id="CAM_NORTH",
            num_radios=2,
            slot_offset_ms=0,
            dwell_limit_ms=400.0,
            min_backoff_ms=1000,
        )
        # Enqueue low priority first, high priority second
        queued = sched.enqueue_frame(keyframe_gen_id=1, frame_seq=10, packets=[low_pkt, high_pkt], priorities=[1, 7])
        self.assertEqual(queued, 2)

        sim = vision_transport.SimulatedLoRaTransport()
        # With 2 radios at DR5, both packets dispatch in one step on distinct channels (0 and 1),
        # with high_pkt (priority 7) assigned first to channel 0
        dispatched = sched.step(now_ms=1000, transport=sim, dr=5)
        self.assertEqual(len(dispatched), 2)
        self.assertEqual(dispatched[0]["channel_idx"], 0)
        self.assertEqual(dispatched[0]["priority"], 7)
        self.assertEqual(dispatched[1]["channel_idx"], 1)
        self.assertEqual(dispatched[1]["priority"], 1)

    def test_scheduler_purges_stale_packets_on_new_keyframe_and_enforces_dwell_guard(self):
        import payload_codec
        import vision_transport

        pkt_old_1 = payload_codec.encode_egdc_packet(2, 1, 0, 2, [{"cell_idx": 1, "mode": 0, "priority": 1, "rgb565": 1}])
        pkt_old_2 = payload_codec.encode_egdc_packet(2, 1, 1, 2, [{"cell_idx": 2, "mode": 0, "priority": 1, "rgb565": 2}])
        pkt_new = payload_codec.encode_egdc_packet(3, 1, 0, 1, [{"cell_idx": 3, "mode": 0, "priority": 5, "rgb565": 3}])

        sched = vision_transport.MultiRadioKeyframeScheduler(camera_id="CAM_EAST", num_radios=1)
        sched.enqueue_frame(keyframe_gen_id=2, frame_seq=1, packets=[pkt_old_1, pkt_old_2], priorities=[1, 1])

        # New keyframe generation 3 arrives -> must purge both gen 2 packets immediately
        purged = sched.reset_for_new_keyframe(new_keyframe_gen_id=3)
        self.assertEqual(purged, 2)
        self.assertEqual(sched.queue_depth(), 0)

        # Enqueue gen 3 packet and verify dwell-time rejection when attempting to send 51B at DR2 (698.4 ms > 400 ms)
        big_cells = [{"cell_idx": i, "mode": 0, "priority": 2, "rgb565": i} for i in range(12)]
        big_pkt = payload_codec.encode_egdc_packet(3, 1, 0, 1, big_cells)  # 6 + 48 = 54B App
        sched.enqueue_frame(keyframe_gen_id=3, frame_seq=1, packets=[big_pkt], priorities=[2])

        sim = vision_transport.SimulatedLoRaTransport()
        dispatched_dr2 = sched.step(now_ms=2000, transport=sim, dr=2)
        self.assertEqual(len(dispatched_dr2), 1)
        self.assertFalse(dispatched_dr2[0]["sent"])
        self.assertEqual(dispatched_dr2[0]["reason"], "dwell_limit_exceeded")

        # Now send valid gen 3 small packet at DR5
        sched.reset_for_new_keyframe(new_keyframe_gen_id=3)
        sched.enqueue_frame(keyframe_gen_id=3, frame_seq=2, packets=[pkt_new], priorities=[5])
        dispatched_dr5 = sched.step(now_ms=3500, transport=sim, dr=5)
        self.assertTrue(dispatched_dr5[0]["sent"])


if __name__ == "__main__":
    unittest.main()
