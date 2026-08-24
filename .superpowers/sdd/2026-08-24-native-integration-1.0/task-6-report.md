# Task 6 Report: Stale Panel Repair And Safe Manual Cleanup

## Delivered

- Added the 30-day `PanelRegistry.stale_macs` threshold on top of Task 5's
  monotonic, contiguous valid-status accounting. It is inclusive at exactly
  30 days and never advances during a disconnected, malformed, or stalled
  status stream.
- Added one fixable warning Repair per stale panel using the stable internal
  issue ID `stale_panel_<entry_id>_<mac_hex>`. Its user-facing text has the
  latest room label, a shortened MAC, and the last valid-report timestamp.
- A valid report clears that panel's stale Repair immediately. Repeated status
  ticks retain one issue instead of multiplying it; ignored/cancelled Repairs
  retain all panel data.
- Added `StalePanelRepairFlow.async_step_confirm()`. It displays an explicit
  confirmation form and only changes data after submission.
- Confirmed cleanup selects entity-registry entries using both the exact config
  entry ID and the stable panel unique-ID prefix. It removes the panel device
  only when no remaining entities or unrelated config entries refer to it,
  then deletes the persistent PanelRecord last.
- A registry error leaves the source panel record and Repair intact for a safe
  retry. The implementation never deletes user automations; it only removes
  entity-registry records owned by this integration.
- A successful deletion schedules a loaded entry reload after source cleanup,
  disposing live dynamic entity-platform state so a subsequent report for the
  same MAC can recreate exactly the original stable entity/device identities.
- Config-entry removal now removes entry-scoped stale Repairs together with the
  existing connection Repairs.
- Diagnostics now present anonymized `panel_01` / `room_01` relationships and
  redact host, credentials, client ID, and technology-system MAC. Full panel
  MACs and room IDs are not exported.

## TDD Evidence

1. Added the threshold and Repair tests first. The first focused run failed
   because `PanelRegistry.stale_macs` and
   `LinkingTempHub.async_sync_stale_panel_repairs()` did not exist.
2. Added the minimal stale threshold property, Repair manager lifecycle, and
   hub report/status boundaries. The focused stale suite then passed.
3. Added a diagnostics privacy test before changing the exporter. It failed
   with the full panel MAC in the diagnostics payload; anonymized export and
   config redaction made it pass.
4. Added the loaded-entry reload assertion before adding scheduling. It failed
   with no scheduled reload, then passed after the cleanup lifecycle was
   completed.

## Verification

- `.venv-313/bin/python -m pytest -q tests/native/test_repairs.py tests/native/test_panel_registry.py tests/native/test_entities.py -k 'stale or delete or rediscover'`: `8 passed, 21 deselected in 0.12s`.
- `.venv-313/bin/python -m pytest -q tests/native`: `108 passed in 0.94s`.
- `.venv-313/bin/python -m compileall -q custom_components/linking_the_world_temp_ha`: passed.
- JSON validation for strings and both translations: passed.
- `git diff --check`: clean.

## Review Fix Round 1

- Added one hub-owned panel lifecycle lock shared by valid panel reports,
  stale-Repair creation, and confirmed stale-panel removal. The confirmation
  path now atomically revalidates that the record still exists, remains
  unavailable, has at least 30 days of observed absence, and still owns the
  matching Repair before any entity/device cleanup starts.
- A report that wins first clears the Repair and makes the old confirmation
  abort with `panel_no_longer_stale`; a confirmation that wins first removes
  the record while holding the same lock, so a waiting report is handled as a
  normal rediscovery after deletion.
- Diagnostics no longer exports raw `last_connection_error` or
  `last_command_status`. It exposes `connection_failure_kind` and a command
  state prefix only, while the existing anonymized panel map and sanitized
  health snapshot remain available for troubleshooting.
- Added canary coverage for IPv4, DNS, IPv6, full panel MAC, room ID, and a
  Chinese room name across the serialized diagnostics payload, plus ordered
  report/confirmation and controlled concurrent deletion/rediscovery tests.
- Verification: `tests/native/test_connection_failures.py`,
  `tests/native/test_health_runtime.py`, and `tests/native/test_repairs.py`
  passed (`49 passed`); all native tests passed (`111 passed`); `compileall`,
  translation JSON validation, and `git diff --check` passed.
