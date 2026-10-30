# ECOWAN Project Architectural & Engineering Rules

## 1. System Overview & Vision
- **Project Name**: ECOWAN (Environmental Monitoring AIoT / LoRaWAN Platform)
- **Primary Objective**: Reliable, modular environmental-monitoring system using ESP32 + SX1262 LoRaWAN sensor nodes (and future STM32/Wio-E5/PLC/Edge AI expansions per project vision), a Dragino LPS8v2 gateway, ChirpStack v4, MQTT, and the custom Edge Monitor web dashboard + firmware code generator.
- **Reliability Philosophy**: Preserve working functionality, diagnose root causes across the full pipeline before editing, make small reversible changes one layer at a time, and handle wireless realities (collisions, duty cycle/dwell time, missed downlinks, sensor faults) gracefully without inventing synthetic frames.
- **KISS Engineering Philosophy ("Keep It Simple, Stupid")**:
  - **Simplicity Over Cleverness**: Build the simplest solution that completely solves the problem. Never introduce speculative abstractions, convoluted multi-layer indirection, or nested wrappers when direct code does the job cleanly.
  - **Automate the Cognitive Burden**: If a task can be determined deterministically by code (e.g., auto-assigning next free wire encoding slots, auto-filtering occupied GPIO pins, sensible defaults), do it automatically without burdening the user with technical minutiae.
  - **Trim the Fat**: Continuously eliminate dead code, unused functions, obsolete legacy folders, and duplicate files. Keep the codebase lean, self-explanatory, and single-purpose.
  - **Single Source of Truth**: Never maintain duplicate copies of configs, catalogs, or templates across multiple directories. Consolidate into clean hubs (e.g., `gateway_hub/`).
  - **Direct, Actionable UI/UX**: Avoid hidden buttons and multi-step traps. Primary actions (such as "Save Configuration" and "Download Code") must be immediately visible, direct, and accessible without hunting through sub-menus or steppers.

## 2. IoT & Node Hardware Constraints
- **MCU & Radio**: ESP32 + SX1262 / RA-01SH (`RadioLib`, `AS923` region plan).
- **SPI & Radio Pins**:
  - `SCK`: GPIO18 | `MISO`: GPIO19 | `MOSI`: GPIO23
  - `NSS/CS`: GPIO5 | `DIO1`: GPIO33 | `RESET`: GPIO27 | `BUSY`: GPIO26
- **Sensor Pins (Standard Nodes `ENODE1` / `ENODE2`)**:
  - `DHT11`: GPIO4 (digital 1-wire temperature & humidity)
  - `HW-038 Soil Moisture`: GPIO34 (input-only `ADC1_CH6` analog output `AO`, 12-bit resolution, `11dB`/`12dB` attenuation; never call `pinMode()` on GPIO34 or exceed 3.3V input)
  - `4-pin Light Sensor Module`: Optional `AO` (ADC-capable GPIO) and/or `D0` digital threshold comparator (e.g., GPIO25 in `latest` demo; verify pin conflict before assigning)
- **Serial Debugging**: `115200` baud.
- **LoRaWAN & Timing Rules**:
  - Allow a **5-second radio settle delay** (`5000 ms`) after `radio.begin()` / `beginOTAA()` setup before calling `activateOTAA()`.
  - Persist `nonces` and `session` buffers in ESP32 `Preferences` (`"lorawan"` namespace). Never casually erase `nonces` or reset `FCnt`, as doing so causes ChirpStack replay/DevNonce rejection.
  - **UTC Slot Scheduler**: Use LoRaWAN `DeviceTimeReq`/`DeviceTimeAns` (`SLOT_PERIOD_MS = 60000`, `ENODE1` offset `5s`, `ENODE2` offset `35s`, `SLOT_GUARD_MS = 3000`). Fall back to randomized jitter when unsynchronized; never send catch-up bursts after a missed slot; always enforce a minimum backoff delay after failed transmissions so the loop never spins.
  - **WLAN Maintenance OTA**: Password-protected SoftAP (`WebServer` on port `80`, `/update` accepting `.bin` only) serialized with `coreMutex` so flash writes and RadioLib SPI/NVS transactions never overlap.

## 3. Payload Codec Standard (EMF1 v1 & Legacy Fixed6)
- **Canonical Format**: **Edge Measurement Format v1 (`EMF1`)** on **FPort `10`**, magic header byte `0xE1`, followed by `[field_id][type_code][big-endian value]` records:
  - `0x01` Temperature: type `0x24` (`int16`, scale `0.01 °C`)
  - `0x02` Humidity: type `0x23` (`uint16`, scale `0.01 %RH`)
  - `0x03` Moisture: type `0x11` (`uint16`, scale `1`, raw ADC counts)
  - `0x10` Core Uptime (Template health check): type `0x31` (`uint32`, scale `1`, seconds)
- **Receive-Only Legacy Compatibility**: **Legacy Fixed6** on **FPort `1`** (6 bytes big-endian: `int16 temp*100`, `uint16 hum*100`, `uint16 moisture`; invalid DHT sentinels `-32768` / `65535`).
- **Missing vs. Zero Values**: Never represent a missing/failed sensor reading as `0`. Omit invalid fields in EMF1 (or use explicit sentinels in Legacy Fixed6) and display missing, invalid, and stale values distinctly in the UI.

## 4. Gateway & Dashboard Architecture
- **Gateway**: Dragino LPS8v2 (reachable via Tailscale at `100.83.203.103`, hostname `dragino-257d3b`).
- **ChirpStack v4**: Runs on gateway; publishes events to local MQTT broker `127.0.0.1:1883` on `application/+/device/+/event/+`.
- **Edge Monitor Service (`edge-monitor.service`)**:
  - Runs `/usr/bin/python3 -u /root/edge_dash.py` serving `http://<gateway-ip>:8026/` (health check at `/healthz`).
  - Live gateway files: `/root/edge_dash.py`, `/root/index.html`, `/root/node_configs.json`, `/root/parts_catalog.json`, `/root/edge_monitor_state.json` (plus `.bak` backups).
  - **Rule**: Never overwrite `/root/edge_dash.py` or `/root/index.html` with node firmware files, and always preserve `.bak` backups before deploying updates.
- **Codegen & Firmware Vision**:
  - Single assembled `.ino` architecture: immutable OTA/LoRaWAN/EMF1 NodeCore + generated modular sensor layer (`buildApplicationFields()`) + sidecar `BUILD_MANIFEST.json`, `node-config.json`, and `chirpstack_codec.js`.

## 5. Security & Diagnostic Procedure
- **Secrets**: Never commit or expose real OTAA `NWK_KEY`/`APP_KEY`, Wi-Fi passwords, MQTT credentials, or `DASH_AUTH_PASSWORD` in public repos or reports.
- **7-Stage Uplink Diagnostic Trace**:
  1. Node Serial (`sendReceive` result & payload hex)
  2. Gateway Packet Forwarder (RF reception, CRC, RSSI/SNR)
  3. ChirpStack v4 (MIC check, `FCnt` validation, codec decode)
  4. MQTT Broker (`application/+/device/+/event/up`)
  5. Dashboard `mosquitto_sub` Ingest (`dedup_key` & `_last_fcnt` checks)
  6. State Persistence (`/root/edge_monitor_state.json`)
  7. Frontend UI Rendering (`index.html` timeline & cards)

## 6. Strict Agent Operational Boundaries & Process Control

All agent sessions working in this repository MUST enforce the following operational boundaries without exception:

### 6.1 Scope Lock
- Identify the user's immediate objective before acting.
- Work **only** on that objective.
- Do not expand into refactoring, redesign, cleanup, unrelated diagnostics, or improvements without explicit approval.
- If a secondary issue blocks the requested task, explain why and ask before broadening scope.

### 6.2 Evidence Before Action
- Inspect relevant source code, logs, configuration, and installed library versions before proposing a root cause.
- Clearly distinguish **confirmed facts**, **hypotheses**, and **unknowns**.
- Never claim that a change fixed an issue unless it has been tested and the results support that conclusion.
- Do not repeat broad investigations after sufficient evidence exists; move to the smallest relevant next step.

### 6.3 Approval Gate
- Before modifying any file, show the proposed file list, exact intended change, rationale, and risk.
- Wait for explicit user approval before editing files, installing/upgrading packages, changing configurations, restarting services, flashing devices, or running migrations.
- Read-only inspection may proceed when relevant and proportionate, but avoid broad scans unrelated to the task.
- After approval, make only the approved changes. If the scope must expand, stop and ask again.

### 6.4 Infrastructure and Persistent-Data Protection
- Never alter, migrate, delete, reset, restore, recreate, or manually edit PostgreSQL, Redis, SQLite, or other persistent data without explicit approval.
- Never restart, reinstall, or reconfigure ChirpStack, MQTT, gateway services, or the dashboard as a speculative fix.
- Do not expose, rotate, regenerate, or replace credentials, keys, nonces, sessions, or SSH access without explicit approval.
- Read-only database queries must remain read-only.
- Do not modify remote gateway files or access controls unless specifically authorized.

### 6.5 Firmware Change Discipline
- Preserve the current working baseline and identify the exact file being compiled.
- Do not modify third-party libraries to compensate for suspected behavior until the relevant source and API behavior have been verified.
- For LoRaWAN scheduling or radio changes, verify the installed RadioLib version and trace the actual execution order before proposing a patch.
- Make one logically isolated change at a time.
- Never flash firmware without explicit approval.
- Preserve existing payload format, FPort, OTAA identity, session/nonce safety, region configuration, and OTA functionality unless a change is explicitly approved.
- Do not reset DevNonce history to work around join failures.

### 6.6 Verification and Reporting
For every approved code change:
- Show the exact diff.
- Explain the expected behavior change.
- Compile or test when possible, clearly reporting what was and was not tested.
- Give a minimal verification procedure and expected logs.
- Do not claim hardware validation based on compilation alone.

### 6.7 Stop Conditions
Stop and ask the user if:
- The task requires changing unrelated components.
- Evidence conflicts or the root cause remains uncertain.
- A proposed operation could affect persistent data, remote access, service availability, or device recovery.
- An unapproved change appears necessary.
- The investigation begins drifting away from the original objective.

### 6.8 Required Response Format (Diagnostic Tasks)
For diagnostic tasks, report using this structure:
1. **Confirmed facts**
2. **Hypothesis and supporting evidence**
3. **Unknowns**
4. **Smallest proposed next action**
5. **Files/services affected and risks**
6. **Approval request**

Keep reports concise and avoid repeating information already established.

## 7. Current Project Context (Reported Diagnostic State — Unverified Until Tested)
- **Active Priority**: Diagnosing missing subsequent LoRaWAN uplinks from `ENode1`.
- **Reported Findings (Treat as reported findings, not independently verified facts until tested)**:
  - Gateway packet capture reportedly shows strong packets at scheduled transmission times, with subsequent packet contents appearing malformed (containing MHDR `0xE0`, which is not a valid ordinary LoRaWAN uplink MHDR).
  - Local unverified edits currently exist in:
    - RadioLib 7.8.1 `LoRaWANNode::transmitUplink()` (moved sleep-delay block before radio staging) and `LoRaWANNode::clearMacCommands()`.
    - `ENODE1/ENODE1.ino` (changed scheduling behavior, added explicit SPI initialization, shortened join retry delay, and added time-sync failure tracking; not compiled or flashed).
    - `/root/.ssh/authorized_keys` on the Dragino gateway (SSH public key added).
  - Do not modify or revert these changes without explicit user instruction and approval.


## 8. Plain-Language Communication & Guided Discovery

These rules supplement the engineering and safety rules above. They do not override the approval gate, security rules, scope lock, or verification requirements.

### 8.1 Explain for a Non-Technical Reader
- Use clear, everyday language by default. Assume the user may not know programming, networking, embedded systems, or command-line terminology.
- Start with the direct answer or current status, then explain only the detail needed to understand the decision.
- When a technical term is necessary, define it briefly the first time it appears. Prefer a familiar analogy when it genuinely helps.
- Avoid unexplained acronyms, jargon, dense paragraphs, and long lists of internal implementation details.
- Separate what is known from what is suspected. Do not present a guess as a fact.
- Keep routine updates short. Give more detail when the user asks, when a decision has meaningful risk, or when the user needs the detail to act safely.

### 8.2 Describe Changes in Plain Language
Whenever proposing or reporting a change, explain:
1. **What will change:** name the file, setting, device, or service in plain language.
2. **Why:** describe the problem the change is intended to solve.
3. **What the user should notice:** explain the expected result without promising it before testing.
4. **Risk and undo plan:** state any meaningful risk and how the change can be reversed.
5. **Verification:** provide the smallest practical check and describe what success and failure look like.

For code changes, also provide the exact diff or a clear summary of the edited lines, in accordance with Section 6.6. Never hide important side effects behind a simplified explanation.

### 8.3 Make Next Steps Detailed, Simple, and Executable
- Give numbered steps in the order the user should perform them.
- Put commands in separate copy-ready code blocks. Say which application or terminal to use and which folder to be in.
- State whether a command only reads information or changes something.
- Avoid combining unrelated actions into one large command when separate steps would be easier to understand or troubleshoot.
- After a step that could fail, explain what output to expect and what to send back if it differs.
- Give only the next useful set of steps; do not bury the user under a long plan when the next result could change the plan.
- Never claim that a command, edit, test, deployment, or fix succeeded unless its result has been observed.

### 8.4 Ask Questions to Build the Missing Context
- Before substantial work, identify what information is missing that could materially affect the solution.
- If the task or project context is incomplete, ask a grouped set of focused questions in one message so the user can answer them together. Make it easy to reply with short answers, “not sure,” or “unknown.”
- Ask about relevant goals, current behavior, expected behavior, environment and versions, what has already been tried, constraints, deadlines, preferred trade-offs, and what must not be changed. Ask only the categories relevant to the task.
- For an ongoing project, ask for useful project context such as the intended users, current architecture, important files, how success is measured, available hardware/services, known limitations, and deployment or recovery constraints.
- Do not ask questions whose answers are already available in the conversation or verified project files. Do not ask the user to repeat information.
- Do not turn every tiny, clear task into an interview. For a small and unambiguous request, proceed within the existing approval rules.
- If enough information is available to make safe progress, do read-only inspection first and ask only for decisions that genuinely require the user's input.
- Never request passwords, API keys, private tokens, private SSH keys, or other secrets. Ask for redacted logs or non-sensitive configuration details instead.
- Do not treat silence or an incomplete answer as permission to perform an action that requires approval under Section 6.3.

### 8.5 Keep the User Oriented
For multi-step tasks, maintain a concise running summary:
- **Goal:** what we are trying to achieve.
- **Confirmed:** what the evidence shows so far.
- **Next step:** the single most useful action now.
- **User decision needed:** only if a decision or approval is required.

Update this summary when the situation changes. Avoid repeating settled facts or restarting broad investigations without a reason.

### 8.6 Respect User Effort and Troubleshooting Scope
- Prefer the smallest diagnostic action that can distinguish between the leading explanations.
- Do not ask for screenshots when a specific command and its output would answer the question more clearly.
- Do not make the user repeat long setup steps if the existing state can be checked first.
- When a previous recommendation was wrong or did not work, acknowledge that plainly, explain what the new evidence changes, and do not defend the failed recommendation.
- Preserve working configurations and create a backup before editing an existing configuration file, where practical.
