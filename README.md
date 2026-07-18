# Moorgen HA Bridge (experimental)

This bridge controls the MC7021 `三恒总控` virtual device through the same local
`yashcp` TCP/9000 protocol used by the Android Moorgen App. It does not contact
Moorgen cloud services and does not emulate an MT8157 device.

The supplied PCAPs prove these operations:

- System power on and off
- Cooling, heat, ventilation, and dehumidify modes
- Home and away scenes
- Winter humidifier on and off

Individual temperature and fan controls are intentionally not exposed yet
because the supplied captures do not contain their write commands.

## Install and start

Run this on a machine that can reach both MC7021 and the Home Assistant MQTT
broker:

```sh
cd moorgen_ha_bridge
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
cp config.example.yaml config.yaml
python3 bridge.py --config config.yaml --debug
```

Set `moorgen.password` to the local MC7021 account password and fill in MQTT
details. Home Assistant's MQTT integration will automatically discover four
entities under `摩根科技系统总控`.

## First test

1. Start the bridge with `--debug`.
2. Confirm it logs `logged in to MC7021` and `connected to MQTT broker`.
3. In Home Assistant, turn on `科技系统总开关`.
4. Verify the App and physical system both reflect the command.

The bridge publishes the raw MC7021 state report at
`moorgen/tech_system/status_raw`. It publishes the requested entity state
immediately after a command; decoding every byte of the host's 14-byte
technology-system status block is a separate follow-up task.

## Device interlocks

- Mode selection is enabled only after the bridge knows the system is off.
  Immediately after a bridge restart the state is deliberately unknown, so
  turn `科技系统总开关` off once before selecting a mode.
- The winter-humidifier entity is present only while the selected mode is
  `heat`. It is removed from MQTT discovery for cooling, ventilation, and
  dehumidify modes.
- The same checks run inside the MQTT command handler, so a direct MQTT
  publish or an old HA automation cannot bypass the UI restrictions.

## Safety notes

- Keep the original mobile App connected during the first test; this bridge is
  a second App-like controller and does not create a virtual panel module.
- Test one control at a time and leave several seconds between commands.
- Do not expose TCP/9000 or MQTT to the public internet.
