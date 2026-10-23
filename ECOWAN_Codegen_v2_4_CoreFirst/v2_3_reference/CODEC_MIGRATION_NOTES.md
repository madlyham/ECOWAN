# EMF1-only codegen: migration notes

## Design decision

EMF1 v1 is the only format the wizard is allowed to generate. The old Legacy Fixed6 path has been removed from the wizard, config validation and firmware scaffold generator. A small Legacy Fixed6 decoder remains at the ingest edge solely so currently installed nodes remain visible before they are deliberately migrated. It is not offered as a configuration option and is never emitted by the generator.

## Canonical registry

`emf1_registry.json` is the canonical registry. It defines:

- standard/version marker `0xE1` and application FPort `10`;
- supported wire types with size, signedness, and numeric scale;
- common field IDs, semantic keys, aliases and units;
- custom ID allocation range and reserved values;
- the fact Legacy Fixed6 is receive-only compatibility.

`payload_codec.py` reads that file at runtime. `build_emf1_artifacts.py` generates the matching registry blocks in the browser UI and ChirpStack JavaScript codec. Run `./build_emf1_artifacts.py --check` after any change; do not edit generated blocks by hand.

## Generator contract

A new configuration must:

1. set `payloadCodec=edge_measurement_v1`, FPort 10 and registry schema `edge-monitor-emf1-registry-v1`;
2. include stable sensor IDs and stable `codecFields` mappings;
3. use the common registry type for common field IDs;
4. set an explicit wire type for each custom field;
5. include the protected `edge-ota-core-v1` block and the EMF1 encoder contract;
6. include a configuration-bound ChirpStack decoder and the generic fallback in the field bundle;
7. include the canonical registry and SHA-256 fingerprint;
8. pass backend-side checks that the browser field map matches the server-normalized map.

The code generator does not generate Legacy Fixed6. Existing config records with that old marker are not evidence that the actual firmware changed. Opening and saving such a profile changes the **intended future build profile** to EMF1 only; it still does not change the device's firmware.

## Out of scope for this step

Individual ESP32 firmware migration; a production build runner; compiled artifact publishing; automated WLAN installation; signed firmware; protected update authentication; recovery and rollback. Do not flash the source scaffold.
