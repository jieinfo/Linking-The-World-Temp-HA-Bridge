# Task 5 Report: Versioned Panel Persistence And Observed Absence

## Delivered

- Added `PanelRegistry` with a version-2 Home Assistant Store while keeping the
  original `linking_the_world_temp_ha.<entry_id>.panels` key intact.
- Migrated v1 `rooms` and `panels` records to stable v2 MAC/room records,
  preserving existing entity and device identities. Migration initializes
  observed absence at zero and writes aware UTC timestamps.
- Moved room and panel persistence out of the hub. The hub keeps its existing
  `thermostats` and `room_names` maps as entity-facing mirrors, so dynamic
  discovery and existing unique IDs are unchanged.
- Observed absence advances only between successive valid controller-status
  reports while a real TCP session is connected and the shared lifecycle is
  `READY`. The first valid status opens observation without backdating time.
- Disconnects, malformed status bodies, decoder malformed-frame events, HA
  shutdown, and later reconnection pauses clear active checkpoints without
  altering stored last-report times or accumulated absence. Fresh panel reports
  alone restore availability and reset that panel's accumulated absence.
- The existing configurable short-term `thermostat_offline_after` continues to
  govern entity availability independently of the 30-day observed-absence
  counter.
- Registry writes are debounced with `Store.async_delay_save`; `async_stop()`
  flushes the newest registry payload before clean unload. Bad v2 records are
  ignored one by one, including invalid MACs/timestamps and boolean/non-finite
  absence values.

## TDD Evidence

1. Added migration and observed-absence tests before `panel_registry.py`
   existed. The first focused run failed during collection with
   `ModuleNotFoundError` for the new registry module.
2. Added the v2 model, storage migration, injected clock, accounting, pause,
   report-reset, and flush behavior; the focused registry tests then passed.
3. Added real hub/runtime tests before wiring the registry into the config
   entry. They failed because both `LinkingTempRuntime` and `LinkingTempHub`
   lacked `panel_registry`.
4. Added malformed transport and disconnected-READY regression tests. Each
   failed before the corresponding hub boundary guard was implemented.
5. Added malformed storage-value cases for boolean and `NaN` absence seconds;
   each failed before validation was tightened.

## Test-Harness Note

- An exploratory same-fixture full config-entry reload test passed its entity
  assertion but left the real Home Assistant test fixture waiting for an active
  long-lived TCP connection during teardown. Process inspection showed the
  wait was outside `Store.async_delay_save`/`async_flush`; storage had already
  written successfully. The test was reduced to the task-owned restart proof:
  v2 flush followed by a fresh registry load plus stable entity unique-ID
  registration. Full reload stress coverage remains intentionally owned by
  Task 8's dedicated lifecycle harness.

## Verification

- `.venv-313/bin/python -m pytest -q tests/native/test_panel_registry.py tests/native/test_setup.py tests/native/test_entities.py -k 'panel or restore or discovery or offline'`: `8 passed, 14 deselected in 0.16s`.
- `.venv-313/bin/python -m pytest -q tests/native`: `95 passed in 0.86s`.
- `.venv-313/bin/python -m compileall -q custom_components/linking_the_world_temp_ha tests/native`: passed.
- `git diff --check`: clean.
- Process check after verification: no residual pytest process; the only matched
  process was the inspection shell itself.

## Review Fix Round 1

- Observed absence now uses an injected process-local monotonic clock. UTC values
  remain persisted as first/last/checkpoint display metadata only; a restarted
  process deliberately has no monotonic checkpoint, so its first valid status
  establishes a fresh baseline without backfilling downtime.
- The hub tracks the last valid controller status separately from generic TCP
  traffic. A status gap beyond `controller_silence_timeout` pauses the registry;
  the recovery status creates a new baseline rather than adding the stalled
  interval. The registry independently applies the same monotonic gap defense.
- Added strict whole-body TLV validation. A legal prefix followed by a truncated
  TLV tail is malformed: no controller state is partially applied and observed
  absence monitoring is paused.
- Short-term thermostat expiry now updates both the entity-facing thermostat
  mirror and the persisted `PanelRecord.available` field. A valid panel report
  restores the record to available; disconnect pauses retain the same mirror
  consistency.
- Delayed store saves remain debounced, and `async_stop()` still flushes the
  latest payload before unload.

### TDD Evidence

1. Added clock-jump, monotonic-gap, strict-truncated-TLV, valid-status-gap, and
   persisted availability tests first. The initial focused run failed with the
   expected missing monotonic constructor/helper/async availability APIs and a
   partially applied `power=ON` state.
2. Implemented the monotonic baselines, strict protocol gate, separate status
   continuity tracker, and synchronized availability persistence. The focused
   regression suite then passed.

### Fix Verification

- `.venv-313/bin/python -m pytest -q tests/native/test_panel_registry.py tests/native/test_setup.py tests/native/test_entities.py -k 'panel or restore or discovery or offline'`: `10 passed, 16 deselected in 0.20s`.
- `.venv-313/bin/python -m pytest tests/native -q`: `100 passed in 0.89s`.
- `.venv-313/bin/python -m compileall -q custom_components/linking_the_world_temp_ha tests/native`: passed.
- `git diff --check`: clean.
