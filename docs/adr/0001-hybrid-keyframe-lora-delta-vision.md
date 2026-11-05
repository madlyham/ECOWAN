# ADR 0001: Hybrid LAN/Internet-Keyframe & LoRa Delta Vision Architecture (`EGDC v2`)

- **Status**: Proposed / Experimental Prototype (Final Pre-Implementation Spec)
- **Date**: 2026-11-03

## Context

ECOWAN is exploring an experimental low-bandwidth visual transport architecture for stationary camera stations (Intel RealSense D435 + Jetson Orin / Pi / Laptop host) starting with **Forest Fire early smoke/flame detection** and extensible to smart infrastructure and emergency monitoring.

Key engineering realities govern this experiment:
1. **Forests Are Dynamic & Early Smoke Is Subtle**: Wind moves branches, clouds shift illumination, shadows sweep across the ground, and rain/fog alters contrast. Furthermore, an unguarded adaptive background reference (`E_ref`) risks slowly absorbing a developing smoke plume into the background.
2. **Radio Budgeting Must Separate `App`, `MAC`, and `PHY` Payloads**:
   - In `AS923`, the usable **Application Payload (`App Bytes`)** differs from the **Physical Payload (`PHY Bytes = App + MAC Overhead`)** by `13–28 bytes` in LoRaWAN (`MHDR + FHDR + FOpts + FPort + MIC`).
   - While `DR4/DR5` (`SF8/SF7`) allow up to `222 App Bytes` (with `FOpts=0`, where we use `160 App Bytes` as a conservative operational target), `DR2` (`SF10`) allows at most `51 App Bytes` (`64 PHY Bytes`).
   - All numerical radio budgets prior to Stage 3 hardware testing are treated as **preliminary engineering estimates** rather than guaranteed operating limits, and separate frequency lanes on an SX1302 do not automatically guarantee collision-free multi-camera reception without cross-camera scheduling.
3. **Packet Count Alone Is Not Transmission Cost**: Four large `Mode 2` packets can cost more airtime than eight tiny `Mode 0` packets.
4. **Sequence Wraparound & Incomplete Updates**: An 8-bit `frame_seq` wraps every 256 frames (~21 minutes at 5s intervals). Without a generation identifier and shadow staging buffer, delayed or lost packets would silently corrupt the reconstructed image.

## Decision

We finalize the **Edge Grid Delta Codec v2 (`EGDC v2`)** experimental architecture with 8 binding design rules:

1. **Masked Environmental Reference Protection (`E_ref` Freeze Mask)**:
   - Normal environmental changes (gradual global illumination shifts, oscillating wind-blown leaves) update `E_ref`.
   - Any cell flagged as a **suspicious / candidate anomaly (`Mode 1` or `Mode 2`)** is **masked out (frozen)** from `E_ref` updates and retains its pre-anomaly baseline observation for comparison.
   - `E_ref` is periodically audited against a clean keyframe so emerging smoke is never learned as the "new normal."

2. **Cost-Based Adaptive Cutoff ($C = T_{\text{airtime}} + T_{\text{queue}} + T_{\text{recovery}}$)**:
   - Alongside configurable packet ceilings (`Conservative=4`, `Balanced=8`, `Extended=10`, `Experimental=16`), the cutoff engine computes total transmission cost $C_{\text{delta}} = T_{\text{airtime}} + T_{\text{queue}} + T_{\text{recovery}}$ using explicit `App + MAC = PHY` byte counts and compares $C_{\text{delta}}$ against the cost of sending a LAN/Internet Keyframe.

3. **Frame Generation ID (`keyframe_gen_id`) & Incomplete-Frame Protection**:
   - The `EGDC v2` 6-byte header carries `[0xE2][keyframe_gen_id][frame_seq][pkt_idx_total][flags_and_cfg][crc8]`.
   - `vision_hub.py` stages incoming packets in a **Shadow Staging Buffer** keyed by `(keyframe_gen_id, frame_seq)`. Stale generation packets are dropped, and incomplete frames **never silently overwrite** the last verified complete image (`committed_frame`).

4. **Strict Codec-Transport Independence**:
   - `EGDC v2` only produces and consumes byte arrays. It is decoupled from the transport layer (`LanSimulatedLoRaTransport` for Stages 1–2, `LoRaWANSerialPoolTransport` Option A, and `CustomLoRaRawTransport` Option B for Stage 3).

5. **Per-Camera & Per-Dongle Transmission Scheduler**:
   - Coordinates transmission slots across both multiple dongles on a single camera station and multiple camera stations sharing the gateway to mitigate RF front-end desensitization and channel collisions.

6. **3-Tier Priority Encoding vs. Validated ML Inference with Uncertainty**:
   - Visual heuristics (low-saturation rising plume morphology) are used strictly to trigger **`Mode 2` High-Detail ROI encoding**, not as fire alerts.
   - Downstream ML models output **Classification + Confidence** and **Regression (`fire_risk_score`) + Uncertainty (`±%`)**, suppressing false alarms during fog, rain, or glare.

7. **5-Scenario Separate Environmental Benchmark Suite (Stage 1 & Stage 2)**:
   - Before touching hardware, the software benchmark suite evaluates `wind_foliage`, `cloud_illumination`, `moving_shadows`, `rain_fog`, and `early_smoke_plume` independently across the `4 / 8 / 10 / 16` profiles.
