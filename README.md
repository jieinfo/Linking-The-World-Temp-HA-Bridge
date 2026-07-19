# Moorgen HA Bridge (experimental)

This bridge controls the MC7021 `三恒总控` virtual device through the same local
`yashcp` TCP/9000 protocol used by the Android Moorgen App. It does not contact
Moorgen cloud services and does not emulate an MT8157 device.

The supplied PCAPs prove these operations on one MC7021 installation:

- System power on and off
- Cooling, heat, ventilation, and dehumidify modes
- Home and away scenes
- Winter humidifier on and off
- Child thermostat enable/disable, target temperature, current temperature, and humidity

Child panels are discovered from their live status reports; their count is not
fixed. The central technology-system mode owns cooling/heating/dehumidification/
ventilation, while each child Climate card only enables or disables that room.

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
details. Use a unique `mqtt.client_id` and `mqtt.topic_prefix` for every home
that shares a broker. Home Assistant's MQTT integration discovers the total
control and every reporting child panel automatically.

## First test

1. Start the bridge with `--debug`.
2. Confirm it logs `logged in to MC7021` and `connected to MQTT broker`.
3. In Home Assistant, turn on `科技系统总开关`.
4. Verify the App and physical system both reflect the command.

The bridge publishes raw MC7021 state reports at
`moorgen/tech_system/status_raw`, so an unsupported installation can be
diagnosed without guessing protocol fields.

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

- For a new household, begin with `safety.allow_control: false`, observe status
  for 24 hours, then explicitly enable control.
- Commands are serialized and rate-limited. The host remains the authority:
  verify its next status report before relying on a change in automations.
- Child panels become unavailable after 900 seconds without a report by default;
  set `safety.thermostat_offline_after: 0` only when a compatible installation
  reports less frequently.
- The bridge is tested against MC7021 local protocol samples only. Do not assume
  compatibility with MT7022, other controller families, or unknown firmware.
- Do not expose TCP/9000 or MQTT to the public internet.
