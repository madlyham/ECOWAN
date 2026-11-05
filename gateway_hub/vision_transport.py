#!/usr/bin/env python3
"""Stage 3: DeltaTransport Abstraction and MultiRadioKeyframeScheduler."""
from __future__ import annotations

import payload_codec


class SimulatedLoRaTransport:
    """Simulated LoRa RF channel with selective packet drop and CRC corruption modeling."""

    def __init__(
        self,
        drop_rate: float = 0.0,
        drop_packet_indices: set[int] | None = None,
        corrupt_packet_indices: set[int] | None = None,
    ):
        self.drop_rate = drop_rate
        self.drop_packet_indices = drop_packet_indices or set()
        self.corrupt_packet_indices = corrupt_packet_indices or set()
        self.inbox: list[bytes] = []

    def send_packet(self, packet: bytes, channel_idx: int = 0, dr: int = 5, now_ms: int = 0) -> dict:
        decoded, err = payload_codec.decode_egdc_packet(packet)
        pkt_idx = decoded.get("pkt_idx") if not err else None

        if pkt_idx is not None and pkt_idx in self.drop_packet_indices:
            return {"sent": False, "reason": "dropped", "packet_idx": pkt_idx}

        data = bytearray(packet)
        if pkt_idx is not None and pkt_idx in self.corrupt_packet_indices:
            data[-1] ^= 0xFF

        self.inbox.append(bytes(data))
        return {
            "sent": True,
            "channel_idx": channel_idx,
            "dr": dr,
            "now_ms": now_ms,
            "packet_idx": pkt_idx,
        }

    def receive_packets(self) -> list[bytes]:
        res = list(self.inbox)
        self.inbox.clear()
        return res


class SerialLoRaTransport:
    """Hardware serial/UART interface stub supporting dry-run verification without hardware."""

    def __init__(self, port: str = "COM1", baudrate: int = 115200, dry_run: bool = False):
        self.port = port
        self.baudrate = baudrate
        self.dry_run = dry_run

    def send_packet(self, packet: bytes, channel_idx: int = 0, dr: int = 5, now_ms: int = 0) -> dict:
        wire_frame = f"AT+SEND={channel_idx},{dr},{packet.hex().upper()}\r\n"
        return {
            "sent": True,
            "dry_run": self.dry_run,
            "port": self.port,
            "channel_idx": channel_idx,
            "dr": dr,
            "now_ms": now_ms,
            "wire_frame": wire_frame,
        }


class MultiRadioKeyframeScheduler:
    """Per-camera scheduler dispatching across multiple radios with priority preemption and dwell guards."""

    def __init__(
        self,
        camera_id: str = "CAM_DEFAULT",
        num_radios: int = 1,
        slot_offset_ms: int = 0,
        dwell_limit_ms: float = 400.0,
        min_backoff_ms: int = 1000,
    ):
        self.camera_id = camera_id
        self.num_radios = num_radios
        self.slot_offset_ms = slot_offset_ms
        self.dwell_limit_ms = dwell_limit_ms
        self.min_backoff_ms = min_backoff_ms
        self.current_keyframe_gen_id: int = 0
        self.queue: list[dict] = []
        self.last_send_ms: dict[int, int] = {i: 0 for i in range(num_radios)}

    def enqueue_frame(
        self,
        keyframe_gen_id: int,
        frame_seq: int,
        packets: list[bytes],
        priorities: list[int] | None = None,
    ) -> int:
        self.current_keyframe_gen_id = keyframe_gen_id
        prio_list = priorities or [1] * len(packets)
        for pkt, prio in zip(packets, prio_list):
            self.queue.append({
                "keyframe_gen_id": keyframe_gen_id,
                "frame_seq": frame_seq,
                "packet": pkt,
                "priority": prio,
            })
        self.queue.sort(key=lambda item: item["priority"], reverse=True)
        return len(packets)

    def queue_depth(self) -> int:
        return len(self.queue)

    def reset_for_new_keyframe(self, new_keyframe_gen_id: int) -> int:
        old_count = len(self.queue)
        self.queue = [item for item in self.queue if item["keyframe_gen_id"] >= new_keyframe_gen_id]
        self.current_keyframe_gen_id = new_keyframe_gen_id
        return old_count - len(self.queue)

    def step(self, now_ms: int, transport, dr: int = 5) -> list[dict]:
        dispatched = []
        for radio_idx in range(self.num_radios):
            if not self.queue:
                break
            candidate = self.queue[0]
            pkt = candidate["packet"]
            app_bytes = len(pkt)
            limit_check = payload_codec.check_as923_payload_limit(app_bytes=app_bytes, dr=dr, dwell_time_limited=True)
            if not limit_check["dwell_400ms_ok"] or not limit_check["valid"]:
                dispatched.append({
                    "sent": False,
                    "channel_idx": radio_idx,
                    "reason": "dwell_limit_exceeded",
                    "priority": candidate["priority"],
                    "airtime_ms": limit_check.get("airtime_ms", 0.0),
                })
                self.queue.pop(0)
                continue

            item = self.queue.pop(0)
            res = transport.send_packet(item["packet"], channel_idx=radio_idx, dr=dr, now_ms=now_ms)
            self.last_send_ms[radio_idx] = now_ms
            dispatched.append({
                "sent": res.get("sent", True),
                "channel_idx": radio_idx,
                "priority": item["priority"],
                "packet": item["packet"],
            })
        return dispatched
