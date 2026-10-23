# Testing the EMF1-only code generator on the Dragino

This procedure intentionally does **not** flash, reconfigure, or contact either ESP32. The individual node firmware sketches are not included in this kit.

## 1. What gets installed where?

- **ChirpStack** receives `chirpstack_codec.js` (JavaScript only) in a Device Profile's payload codec editor.
- **Edge Monitor dashboard** uses `payload_codec.py` and the code generator in `edge_dash.py` + `index.html`.
- **Test harness** uses `test_payload_codec.py`, `test_chirpstack_codec.js`, `codec_test_vectors.json`, and `run_codec_tests.sh`.

Do not paste the Python file into ChirpStack. Do not paste only a fragment of the JavaScript codec: use the complete `chirpstack_codec.js` file.

## 2. Safe first check inside ChirpStack

On an LPS8v2, Dragino documents its bundled ChirpStack v4 on the gateway's web interface (commonly reachable at `http://<DRAGINO-IP>:8080`, or by using the gateway's Launch button). Your exact firmware/build may differ.

1. Open ChirpStack and navigate to **Device profiles**.
2. Identify the profile used by ENode1 / ENode2. Check whether unrelated device models also use it.
3. Before changing anything, copy the existing codec script into a local text file as a rollback backup. Keep the profile's LoRaWAN region, MAC version, activation mode, and other radio settings untouched.
4. Open the profile's **Codec** tab. Select **Custom JavaScript codec functions** and paste the entire `chirpstack_codec.js` contents; save.
5. The decoder reads Legacy Fixed6 on FPort 1 (exactly six bytes) and EMF1 on FPort 10 (header `E1`). The wizard and source generator emit EMF1 only. This receive-only compatibility keeps currently installed six-byte nodes visible until they are deliberately migrated. Confirm the actual `fPort` shown in the device's Events before interpreting results.
6. Open the device's **Events** and inspect a later `up` event. Verify both the raw payload and the decoded `object`. For the known six-byte sample, hex `0B 40 0C 80 00 00` on FPort 1 should decode to temperature `28.8`, humidity `32`, moisture `0`.

**Shared-profile caution:** if other unrelated devices use the same profile, do not overwrite their codec with this one. Create a separate profile with equivalent radio settings and use it only for an appropriate test device, or defer changing ChirpStack until the profile can be isolated. Do not change live device activation/radio settings just to test a codec.

## 3. Test EMF1 with no physical node

ChirpStack v4's Device Profile codec editor is not a general-purpose manual uplink simulator. Test the EMF1 bytes with the included repeatable harness first, then install the tested decoder in ChirpStack. The LPS8v2 has a Node-RED/Node.js component on many firmware builds, but check availability instead of assuming it exists.

To test without changing the running dashboard:

1. Download this ZIP to a computer that can reach the gateway.
2. Copy it to the Dragino using your normal SSH/SCP/SFTP method, or test it on your own computer first.
3. Extract it into a temporary location such as `/tmp/edge-monitor-emf1-test-kit`, **not** over `/root/edge_dash.py` or the live dashboard directory.
4. From the extracted kit directory run:

   ```sh
   ./run_codec_tests.sh
   ```

   This runs Python syntax checks, the Python codec/codegen unit tests, validates shared golden-vector fixtures, and—if `node` exists—runs the JavaScript codec tests. It does not restart any service, alter ChirpStack, or contact the nodes.

If Node.js is unavailable, the script reports that the JavaScript test was skipped. Run the JavaScript tests on a computer with Node.js (`node --check chirpstack_codec.js` and `node test_chirpstack_codec.js`) rather than treating the skipped test as a full pass.

## 4. Canonical test frame

EMF1 test frame:

- FPort: `10`
- Hex: `E1 01 24 0B 40 02 23 0C 80 03 11 00 00`
- Expected decoded object:

  ```json
  {
    "temperature": 28.8,
    "humidity": 32,
    "moisture": 0,
    "payload_format": "edge_measurement_v1"
  }
  ```

The exact same bytes, FPort, and expected object are in `codec_test_vectors.json`; update that fixture first when adding a new standard case, and add assertions in both language test suites as needed. Never silently change the expected output to fit an incorrect decoder.

## 5. Current status and rollback

- Current known ENode1/ENode2 firmware uses the six-byte Legacy Fixed6 payload; do not assume it emits EMF1 just because the dashboard profile is set to EMF1.
- The code generator's field-bundle download includes a node/configuration-specific `chirpstack_codec.js` and a generic fallback. For configured custom fields, use the field-specific script for that node's profile; the generic script labels unknown custom IDs as `field_XX`.
- The canonical registry is `emf1_registry.json`. After changing it on a development copy, run `./build_emf1_artifacts.py` to regenerate the browser/ChirpStack registry fragments and `./run_codec_tests.sh` to verify they are synchronized. Do not hand-edit a generated marker section.
- The generated source is one assembled `.ino`: the fixed OTA prefix precedes the generated EMF1/sensor application. The field bundle places it at `firmware/<SketchName>/<SketchName>.ino` and includes `BUILD_MANIFEST.json` hashes.
- The generated source remains uncompiled; passing these tests validates codec/codegen/source assembly, not a flash-ready firmware image.
- No compiler service, `.bin` generation, automatic WLAN push, or physical-node migration is included in this test kit.
- To roll back a profile codec change, paste the backup of the old codec script into the same profile Codec editor and save.

## 6. Documentation references

- ChirpStack v4 Device Profiles / custom JavaScript codec: https://www.chirpstack.io/docs/chirpstack/use/device-profiles.html
- Dragino LPS8v2 built-in ChirpStack details: https://www.wiki.dragino.com/docs/LoRaWAN-Gateway/indoor-gateways/lps8v2/
- ChirpStack discussion of testing codecs without physical uplinks: https://forum.chirpstack.io/t/test-a-codec-on-a-chirpstack-v4-fresh-install/22085


The dashboard backend also checks `ota_core_contract.json` every time it returns the standalone `.ino` or field bundle. A modified OTA block is rejected instead of silently being treated as `edge-ota-core-v1`. An intentional maintenance-core change needs an explicit version/hash update and tests.
