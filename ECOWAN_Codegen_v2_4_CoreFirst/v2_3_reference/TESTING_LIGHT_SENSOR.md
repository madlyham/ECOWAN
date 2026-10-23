# ECOWAN 4-pin light sensor generator test

## Module model

Common four-pin module: VCC, GND, AO (analog output), DO (digital comparator output).

- Select **4-pin light sensor module (AO + DO)**.
- Protocol: **Analog voltage / ADC**.
- Interface / port: select the ESP32 ADC-capable pin connected to AO (e.g. GPIO32 or GPIO34, subject to the actual board wiring and other modules).
- Bus address / light sensor D0 pin: enter `GPIO25` (or another free safe GPIO) if DO is connected; leave blank for AO-only mode.
- Keep the module output voltage within ESP32 input limits. Confirm whether the particular module's AO/DO can rise to its supply voltage before powering it.

## Expected generated logic

- AO: `analogRead(AO_PIN)`, emitted as raw ADC counts under `light_adc`.
- DO: `digitalRead(D0_PIN)`, emitted as boolean state under `light_digital_state` only when a D0 pin is configured.
- DO is a threshold/comparator output controlled by the module's trimmer; it is not a lux measurement.
- No lux conversion or soil-moisture-style percentage is inferred.

## Manual capability test checklist

- [ ] Generator lists the new module.
- [ ] Selecting it shows the analog pin selector and optional D0 pin input.
- [ ] Generated `.ino` contains `analogRead()` for AO and `digitalRead()` for configured DO.
- [ ] Generated EMF1 fields have unique IDs and correct types (unsigned 16-bit raw ADC; boolean state).
- [ ] Duplicate pin / radio-reserved pin warnings are reviewed.
- [ ] Inspect serial output with the sensor in bright and dark conditions; verify AO changes and DO changes when the threshold trimmer is crossed.
- [ ] Confirm ChirpStack decoder field IDs/keys match the generated field package.

## Hard pre-compiler gate

This is a sensor-generation capability test only. The current generated sketch is still a source scaffold: it does **not** implement the actual SX1262 RadioLib OTAA lifecycle, persisted DevNonce/session state, or `sendReceive(..., FPort 10)` integration. Do not compile/flash it as a replacement for the working NodeCore firmware until that shared core is integrated and tested. The compiler must refuse publication while this gate is unresolved.
