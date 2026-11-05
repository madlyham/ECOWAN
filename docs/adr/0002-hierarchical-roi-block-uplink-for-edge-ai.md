# ADR 0002: Hierarchical 2-Stage Vision Pipeline (Coarse Grid Trigger + Selective High-Res ROI Block Uplink)

## Status
Approved for Architecture Planning (Proposed by User, 2026-11-05).

## Context & Motivation
Transmitting full high-resolution camera frames (e.g., 640x480 RGB at ~45,000 to 900,000 bytes) over LPWAN/LoRa channels violates regional duty cycles (AS923 400 ms dwell ceiling) and causes severe RF collisions.
Conversely, transmitting only coarse low-resolution averages (such as our 32x24 grid cells) compresses bandwidth by 99%+, but loses fine textural details needed by deep learning classifiers (e.g. distinguishing actual wildfire smoke plume vorticity from tractor dust or cloud shadows).

## Architecture Decision
Implement a **Hierarchical 2-Stage Vision Architecture**:

### 1. Stage 1 — Coarse Edge Grid Detection (Stationary Node)
- The stationary camera node downsamples the camera frame to a $32 \times 24$ coarse grid (each cell corresponds to a $20 \times 20$ pixel region in native 640x480).
- Normalizes global ambient illumination against the frozen environmental reference $E_{ref}$.
- Evaluates deadband ($T_{noise}$) and local variance.
- **Trigger**: Only cells exhibiting significant change ($\Delta > T_{noise}$ or variance $> 15.0$) are flagged as candidate anomalous blocks.
- **Zero RF Guarantee**: If no blocks exceed the threshold, zero radio packets are sent.

### 2. Stage 2 — Foveated High-Res ROI Block Slicing & Uplink
- Instead of transmitting the entire 640x480 frame, the node slices *only the high-resolution native pixels corresponding to the flagged anomalous blocks*.
- For a detected 4-block plume:
  $$\text{Payload} = 4 \times (20 \times 20) = 1600 \text{ pixels}$$
- Compressed via lightweight run-length or discrete cosine transform (JPEG patch) to $\le 300\text{--}400$ bytes total.
- Transmitted as an EGDC Mode 2/3 packet array carrying `[block_index][compressed_patch_bytes]`.

### 3. Stage 3 — Jetson Orin Patch Stitching & AI Classification
- The remote Jetson Orin edge server maintains a high-resolution reference background canvas.
- Upon receiving the uplinked block patches, the Orin reconstructor stitches the high-res pixels into the reference canvas at the exact cell coordinates.
- Slices the bounding box containing the updated patches and feeds it directly into the AI classification model (YOLOv8 / ResNet smoke detector).
- Outputs verified anomaly classification with bounding box, smoke plume probability, and wind trajectory.

## Benefits & Invariants
- **Airtime Compliance**: Keeps transmission within regional LoRaWAN dwell limits (< 400 ms).
- **Zero False Alarm Jamming**: Normal days with no movement use 0 ms airtime.
- **High-Fidelity AI Inference**: The Orin classifier receives crystal-clear native pixel resolution of the smoke plume without needing full-frame video streaming.
