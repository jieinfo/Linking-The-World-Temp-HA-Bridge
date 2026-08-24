# Linking The World Temp HA 1.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release the native HACS integration as `1.0.0` with real Home Assistant tests, typed connection/authentication behavior, actionable Repairs, manual stale-panel removal, and privacy-safe diagnostics.

**Architecture:** Keep one push TCP session per config entry, but move protocol failures, health metrics, panel persistence, and Repairs into focused modules. Store the resulting typed runtime object in `ConfigEntry.runtime_data`; use a programmable fake MC7021 server and a real Home Assistant pytest runtime to verify behavior end to end.

**Tech Stack:** Python 3.13, Home Assistant 2026.8+, asyncio TCP, voluptuous, Home Assistant config entries/Repairs/device and entity registries/storage, pytest, pytest-homeassistant-custom-component, pytest-cov, HACS validation, hassfest.

**Spec:** `docs/superpowers/specs/2026-08-24-native-integration-1.0-design.md`

## Global Constraints

- Minimum supported Home Assistant version is exactly `2026.8.0`.
- Preserve existing config-entry unique IDs, entity unique IDs, device identifiers, modes, commands, and dynamic discovery behavior.
- Never infer invalid credentials from a timeout, unknown reply, socket close, or undocumented field value.
- Never delete a stale panel automatically; deletion requires a completed Repair confirmation flow.
- Count stale-panel absence only while the controller is authenticated and the status stream is demonstrably alive.
- Diagnostic exports must not contain host, username, password, client ID, real MACs, room names, keys, tokens, or raw frames.
- Production logging must not emit raw protocol frames.
- Use test-driven changes and keep each task independently passing before continuing.

---

## File Structure

### New integration modules

- `custom_components/linking_the_world_temp_ha/runtime.py`: typed config-entry runtime container and lifecycle/failure enums.
- `custom_components/linking_the_world_temp_ha/health.py`: bounded counters, timestamps, latency summaries, and sanitized snapshots.
- `custom_components/linking_the_world_temp_ha/panel_registry.py`: panel store v2, migration, absence accounting, and registry deletion.
- `custom_components/linking_the_world_temp_ha/repairs.py`: issue creation/deletion and Repair flows.

### Test support

- `requirements_test.txt`: HA pytest runtime and coverage dependencies.
- `pytest.ini`: asyncio/custom-component test configuration.
- `tests/conftest.py`: real HA fixtures and default config entry.
- `tests/helpers.py`: programmable fake MC7021 TCP server and protocol frame builders.
- `tests/native/test_setup.py`: setup, unload, reload, runtime-data, and restoration tests.
- `tests/native/test_config_flow.py`: real user/reconfigure/options/reauth flow tests.
- `tests/native/test_connection_failures.py`: stage classification, reconnect, and issue lifecycle tests.
- `tests/native/test_entities.py`: dynamic entity and control behavior tests.
- `tests/native/test_panel_registry.py`: store migration and observed-absence tests.
- `tests/native/test_repairs.py`: Repair confirmation and HA registry cleanup tests.
- `tests/native/test_diagnostics.py`: metric and redaction tests.

Existing focused pure tests for protocol decoding, command queuing, temperature serialization, and thermostat policy remain in place and are converted to pytest style only when required by the real HA fixture stack.

---

### Task 1: Establish The Real Home Assistant Test Runtime

**Files:**
- Create: `requirements_test.txt`
- Create: `pytest.ini`
- Create: `tests/conftest.py`
- Create: `tests/helpers.py`
- Create: `tests/native/test_setup.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `FakeMC7021Server`, `FakeControllerBehavior`, `mock_config_entry`, `setup_integration`.
- `FakeMC7021Server.host: str`, `port: int`, `received_frames: list[YasHcpFrame]`.
- `FakeMC7021Server.async_send_status(body: bytes) -> None` sends a valid status frame to the connected client.
- `FakeControllerBehavior` exposes `hello_reply`, `login_reply`, `close_after_stage`, and per-stage delays.

- [ ] **Step 1: Add a failing real-HA setup test**

```python
async def test_setup_uses_real_home_assistant(hass, mock_config_entry, fake_controller):
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
```

- [ ] **Step 2: Run the test and verify the current environment cannot satisfy it**

Run: `pytest -q tests/native/test_setup.py::test_setup_uses_real_home_assistant`

Expected: FAIL because the HA pytest fixtures and fake controller do not exist.

- [ ] **Step 3: Add deterministic test dependencies and pytest configuration**

`requirements_test.txt` must pin `pytest-homeassistant-custom-component==0.13.357`
(Home Assistant `2026.8.3`) and `pytest-cov==6.2.1`; the HA test package
supplies its compatible pytest and asyncio dependencies. `pytest.ini` must set
`asyncio_mode = auto`, register `enable_custom_integrations`, and restrict
default discovery to `tests`.

- [ ] **Step 4: Implement the fake controller**

Build replies with the production `YasHcpFrame`/TLV helpers. The server must parse incoming frames through `YasHcpDecoder`, reply to hello `(1, 1)` with `(1, 3)`, reply to login `(2, 4)` with `(2, 6)`, acknowledge status requests, and support delayed, fragmented, concatenated, malformed, and closed connections without using sleeps in individual tests.

- [ ] **Step 5: Add shared HA fixtures**

Create a `MockConfigEntry` with host/port supplied by the fake server, username `admin`, password `secret`, the current client ID and total MAC, plus default options. `setup_integration` must add the entry, call `async_setup`, block until loaded, and return `entry.runtime_data`.

- [ ] **Step 6: Run the new real-HA baseline and existing pure tests**

Run: `pytest -q tests/native/test_setup.py tests/native/test_protocol.py tests/native/test_command_queue.py`

Expected: PASS with no skipped tests.

- [ ] **Step 7: Update CI to install test requirements and run pytest**

Keep add-on syntax/build checks, then add a native job that installs `requirements_test.txt` and runs `pytest --cov=custom_components/linking_the_world_temp_ha --cov-report=term-missing`. Do not set the 95% gate until Task 8 adds full coverage.

- [ ] **Step 8: Commit**

```bash
git add requirements_test.txt pytest.ini tests .github/workflows/ci.yml
git commit -m "test: add real Home Assistant runtime"
```

---

### Task 2: Introduce Typed Protocol Failures

**Files:**
- Modify: `custom_components/linking_the_world_temp_ha/protocol.py`
- Modify: `tests/native/test_protocol.py`
- Create: `tests/native/test_connection_failures.py`

**Interfaces:**
- Produces exception hierarchy:

```python
class MoorgenConnectionError(Exception): ...
class TcpConnectError(MoorgenConnectionError): ...
class HandshakeTimeout(MoorgenConnectionError): ...
class LoginTimeout(MoorgenConnectionError): ...
class AuthenticationRejected(MoorgenConnectionError): ...
class IncompatibleProtocol(MoorgenConnectionError): ...
```

- Produces `AsyncMoorgenClient.connect() -> None` with stage-specific exceptions.
- Explicit rejection is raised only by a documented `is_explicit_auth_rejection(frame)` predicate. The default predicate returns false for every currently captured response because no captured rejection marker exists.

- [ ] **Step 1: Write failing exception-classification tests**

Test TCP refusal, missing hello reply, missing login reply, malformed hello reply, and an injected explicit rejection detector. Assert that unknown login bodies and closed sockets do not become `AuthenticationRejected`.

- [ ] **Step 2: Run focused tests and observe generic `CannotConnect` failures**

Run: `pytest -q tests/native/test_connection_failures.py -k protocol`

Expected: FAIL because the typed exceptions do not exist.

- [ ] **Step 3: Implement stage-specific connection handling**

Split socket open, hello wait, and login wait into private methods that wrap only their own expected failures. Preserve original exceptions with `raise ... from error`. Keep `CannotConnect` as a compatibility alias of `MoorgenConnectionError` until all callers move in later tasks.

- [ ] **Step 4: Preserve the conservative authentication rule**

Add a rejection-detector injection point for tests, but do not assign semantics to undocumented production fields. A captured successful login reply must pass. Unknown replies, EOF, and timeout must map to `LoginTimeout` or `IncompatibleProtocol`, never invalid authentication.

- [ ] **Step 5: Verify framing regressions**

Run: `pytest -q tests/native/test_protocol.py tests/native/test_connection_failures.py -k 'protocol or framing or stream'`

Expected: PASS, including fragmented/concatenated/resynchronized frames.

- [ ] **Step 6: Commit**

```bash
git add custom_components/linking_the_world_temp_ha/protocol.py tests/native
git commit -m "refactor: classify controller protocol failures"
```

---

### Task 3: Add Typed Runtime State And Health Metrics

**Files:**
- Create: `custom_components/linking_the_world_temp_ha/runtime.py`
- Create: `custom_components/linking_the_world_temp_ha/health.py`
- Modify: `custom_components/linking_the_world_temp_ha/__init__.py`
- Modify: `custom_components/linking_the_world_temp_ha/hub.py`
- Modify: `custom_components/linking_the_world_temp_ha/{binary_sensor,climate,select,sensor,switch}.py`
- Modify: `custom_components/linking_the_world_temp_ha/entity.py`
- Modify: `tests/native/test_setup.py`
- Modify: `tests/native/test_connection_failures.py`

**Interfaces:**

```python
class ConnectionStage(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    AUTHENTICATING = "authenticating"
    READY = "ready"

class FailureKind(StrEnum):
    NONE = "none"
    TCP_REFUSED = "tcp_refused"
    TCP_TIMEOUT = "tcp_timeout"
    HANDSHAKE = "handshake_failed"
    LOGIN_TIMEOUT = "login_timeout"
    AUTH_REJECTED = "authentication_rejected"
    PROTOCOL = "protocol_incompatible"
    STATUS_SILENCE = "status_silence"

@dataclass(slots=True)
class LinkingTempRuntime:
    hub: LinkingTempHub
    health: HealthTracker

type LinkingTempConfigEntry = ConfigEntry[LinkingTempRuntime]
```

`HealthTracker` provides `increment(name)`, `record_failure(kind, message)`, `record_confirmation_latency(seconds)`, `mark_stage(stage)`, and `snapshot()`. Histories use bounded deques and counters; messages are sanitized before storage.

- [ ] **Step 1: Write failing runtime and state-machine tests**

Assert `entry.runtime_data.hub` exists, all platforms access runtime data, connection binary sensor is off until `READY`, and stage transitions occur in order during setup.

- [ ] **Step 2: Run tests and verify `hass.data` and Boolean connection state fail expectations**

Run: `pytest -q tests/native/test_setup.py tests/native/test_connection_failures.py -k 'runtime or stage or connection_sensor'`

- [ ] **Step 3: Implement lifecycle enums and health tracker**

Counters must include connection attempts/successes/disconnects/reconnects, handshake and login outcomes, frames decoded/malformed/resynchronized, invalid measurements, commands sent/confirmed/retried/coalesced/blocked/timed out, and confirmation latencies. Snapshot collections must be bounded.

- [ ] **Step 4: Migrate setup and platform access to `runtime_data`**

Assign `entry.runtime_data` only after runtime construction succeeds. Unload platforms, call `entry.runtime_data.hub.async_stop()`, and rely on HA to clear runtime data. Replace every `hass.data[DOMAIN][entry.entry_id]` platform lookup with `entry.runtime_data.hub`.

- [ ] **Step 5: Drive state transitions from protocol callbacks**

The hub updates `ConnectionStage` before socket open, hello, login, and ready. `available` is true only in `READY` with verified status. Disconnect moves to `DISCONNECTED` before notifying entities. Preserve command queue clearing and room thermostat availability behavior.

- [ ] **Step 6: Record command and parser health events**

Wire the health tracker to sends, coalescing, confirmations, retries, timeouts, invalid measurements, disconnects, and parser resynchronization counters. No metric path may log or store a frame body.

- [ ] **Step 7: Run lifecycle, entity, queue, and task-leak tests**

Run: `pytest -q tests/native/test_setup.py tests/native/test_connection_failures.py tests/native/test_task_lifecycle.py tests/native/test_command_queue.py`

Expected: PASS and zero pending-task warnings after unload.

- [ ] **Step 8: Commit**

```bash
git add custom_components/linking_the_world_temp_ha tests/native
git commit -m "refactor: add typed runtime and connection health"
```

---

### Task 4: Implement Reauthentication And Actionable Connection Repairs

**Files:**
- Create: `custom_components/linking_the_world_temp_ha/repairs.py`
- Modify: `custom_components/linking_the_world_temp_ha/config_flow.py`
- Modify: `custom_components/linking_the_world_temp_ha/hub.py`
- Modify: `custom_components/linking_the_world_temp_ha/strings.json`
- Modify: `custom_components/linking_the_world_temp_ha/translations/en.json`
- Modify: `custom_components/linking_the_world_temp_ha/translations/zh-Hans.json`
- Modify: `tests/native/test_config_flow.py`
- Modify: `tests/native/test_connection_failures.py`
- Create: `tests/native/test_repairs.py`

**Interfaces:**
- `RepairManager.async_set_login_timeout(active: bool) -> None`
- `RepairManager.async_set_protocol_incompatible(active: bool) -> None`
- Issue IDs: `login_timeout_<entry_id>` and `protocol_incompatible_<entry_id>`.
- Reauth steps: `async_step_reauth(entry_data)` and `async_step_reauth_confirm(user_input)`.

- [ ] **Step 1: Write failing real config-flow tests**

Use `hass.config_entries.flow.async_init` to test successful user setup, each typed error key, reconfigure without duplicate entries, explicit-auth reauth, wrong reauth credentials, and successful credential replacement. Assert reauth updates the same entry.

- [ ] **Step 2: Write failing issue lifecycle tests**

Assert three consecutive `LoginTimeout` events create one fixable warning issue, one or two do not, success removes it, repeated creation is deduplicated, protocol incompatibility uses a separate issue, and TCP failures do not create either issue.

- [ ] **Step 3: Implement localized config-flow error mapping**

Map typed failures to `cannot_connect`, `handshake_failed`, `login_timeout`, `invalid_auth`, and `protocol_incompatible`. Keep validation failures under `invalid_config`; unexpected errors remain `unknown` with one traceback.

- [ ] **Step 4: Implement reauth**

Show only username and password, merge them with the existing host/port/client/MAC data, validate, call `_abort_if_unique_id_mismatch()`, then `async_update_reload_and_abort(..., data_updates=credentials)`. At runtime call `entry.async_start_reauth(hass)` only for `AuthenticationRejected`.

- [ ] **Step 5: Implement connection issue management**

Create warning Repairs with translation placeholders and config-entry linkage. Login timeout issue appears at the third consecutive timeout. Any successful authenticated session clears the counter and issue. Protocol issue clears only after a compatible authenticated session.

- [ ] **Step 6: Add complete English and Simplified Chinese text**

Include config errors, reauth form and success message, issue titles/descriptions, and Repair instructions. Custom integration translation files must contain flat final text rather than Home Assistant Core placeholder references.

- [ ] **Step 7: Run config and Repair tests**

Run: `pytest -q tests/native/test_config_flow.py tests/native/test_connection_failures.py tests/native/test_repairs.py -k 'auth or login or protocol or config'`

Expected: PASS with no duplicate flows or issues.

- [ ] **Step 8: Commit**

```bash
git add custom_components/linking_the_world_temp_ha tests/native
git commit -m "feat: add reauthentication and connection repairs"
```

---

### Task 5: Add Versioned Panel Persistence And Observed Absence Accounting

**Files:**
- Create: `custom_components/linking_the_world_temp_ha/panel_registry.py`
- Modify: `custom_components/linking_the_world_temp_ha/hub.py`
- Modify: `custom_components/linking_the_world_temp_ha/runtime.py`
- Create: `tests/native/test_panel_registry.py`
- Modify: `tests/native/test_setup.py`
- Modify: `tests/native/test_entities.py`

**Interfaces:**

```python
@dataclass(slots=True)
class PanelRecord:
    mac_hex: str
    room_id: str
    first_seen_utc: datetime
    last_report_utc: datetime | None
    available: bool
    monitored_absence_seconds: float
    checkpoint_utc: datetime | None

class PanelRegistry:
    async def async_load(self) -> None: ...
    async def async_note_status_stream(self, now_utc: datetime) -> None: ...
    async def async_note_panel_report(self, mac_hex: str, room_id: str, now_utc: datetime) -> bool: ...
    async def async_pause_monitoring(self, now_utc: datetime) -> None: ...
    async def async_flush(self) -> None: ...
    async def async_delete_panel(self, mac_hex: str) -> None: ...
```

- Store key remains `f"{DOMAIN}.{entry.entry_id}.panels"` so existing data is found.
- Home Assistant `Store` version becomes `2`; v1 migration initializes monitored absence to zero.
- Stale threshold is exactly `30 * 24 * 60 * 60` observed seconds.

- [ ] **Step 1: Write failing store migration tests**

Seed the current v1 payload (`rooms`, `panels` with `mac` and `room_id`), load it, and assert stable MAC/room data, timezone-aware timestamps, zero observed absence, and no immediate Repair.

- [ ] **Step 2: Write failing accounting boundary tests**

Use an injected clock. Verify READY plus valid status traffic increments absence, HA/controller downtime does not, reconnect does not reset prior accumulation, malformed/stalled traffic pauses it, and a valid report resets it to zero.

- [ ] **Step 3: Implement the v2 record model and migration**

Use ISO-8601 UTC strings on disk. Reject malformed records individually without discarding valid records. Debounce writes through `Store.async_delay_save`; flush on clean unload.

- [ ] **Step 4: Move panel and room persistence out of the hub**

Hub status decoding calls `async_note_status_stream` for valid controller status and `async_note_panel_report` for valid thermostat status. Hub mirrors registry records into existing `thermostats` and `room_names` structures so entity interfaces and unique IDs remain unchanged.

- [ ] **Step 5: Preserve short-term panel availability behavior**

Keep the configurable `thermostat_offline_after` entity-availability timeout. It is independent from the 30-day deletion eligibility counter. A TCP disconnect marks every panel unavailable but does not reset either last report or accumulated absence.

- [ ] **Step 6: Verify migration, restart, and dynamic discovery**

Run: `pytest -q tests/native/test_panel_registry.py tests/native/test_setup.py tests/native/test_entities.py -k 'panel or restore or discovery or offline'`

Expected: PASS without duplicate entity unique IDs after reload.

- [ ] **Step 7: Commit**

```bash
git add custom_components/linking_the_world_temp_ha/panel_registry.py custom_components/linking_the_world_temp_ha/hub.py custom_components/linking_the_world_temp_ha/runtime.py tests/native
git commit -m "feat: track persistent panel lifecycle"
```

---

### Task 6: Add Manual Stale-Panel Repair And Registry Cleanup

**Files:**
- Modify: `custom_components/linking_the_world_temp_ha/repairs.py`
- Modify: `custom_components/linking_the_world_temp_ha/panel_registry.py`
- Modify: `custom_components/linking_the_world_temp_ha/hub.py`
- Modify: `custom_components/linking_the_world_temp_ha/strings.json`
- Modify: `custom_components/linking_the_world_temp_ha/translations/en.json`
- Modify: `custom_components/linking_the_world_temp_ha/translations/zh-Hans.json`
- Modify: `tests/native/test_repairs.py`
- Modify: `tests/native/test_panel_registry.py`

**Interfaces:**
- Issue ID: `stale_panel_<entry_id>_<mac_hex>`; raw MAC is allowed in the internal issue ID but never in translated display text or exported diagnostics.
- Issue `data` contains `entry_id` and `mac_hex` for the flow, plus display placeholders for room, shortened MAC, and last report.
- `StalePanelRepairFlow.async_step_confirm()` performs one explicit deletion and returns an empty repair entry.

- [ ] **Step 1: Write failing 30-day issue tests**

Advance the injected clock to one second before and at the exact threshold. Assert no issue before, one fixable warning issue at the threshold, no duplicates on later ticks, and automatic issue removal if a valid report returns.

- [ ] **Step 2: Write failing deletion-flow tests**

Register a thermostat climate entity, automation sensors, and panel device. Run the Repair flow, cancel once and assert nothing changes, then confirm and assert panel store, entity registry entries, device registry entry, and issue are removed.

- [ ] **Step 3: Implement stale issue creation and clearing**

Create one issue per panel with localized room, shortened MAC, and last-report placeholders. Retain ignored issues until the panel reports again or the user completes deletion, matching HA issue-registry behavior.

- [ ] **Step 4: Implement safe registry cleanup**

Find entities by `config_entry_id` and the exact stable panel unique-ID prefix, remove those entities, then remove the panel device only when no unrelated entries remain. Delete persisted panel state last so a partial registry failure cannot silently lose the source record.

- [ ] **Step 5: Verify rediscovery**

After confirmed deletion, send a valid report for the same panel. Assert exactly one device and one set of entities are recreated with the original stable identifiers and no stale Repair.

- [ ] **Step 6: Run Repair and lifecycle tests**

Run: `pytest -q tests/native/test_repairs.py tests/native/test_panel_registry.py tests/native/test_entities.py -k 'stale or delete or rediscover'`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add custom_components/linking_the_world_temp_ha tests/native
git commit -m "feat: add manual stale panel cleanup"
```

---

### Task 7: Publish Privacy-Safe Diagnostics And Clear Diagnostic Entities

**Files:**
- Modify: `custom_components/linking_the_world_temp_ha/diagnostics.py`
- Modify: `custom_components/linking_the_world_temp_ha/health.py`
- Modify: `custom_components/linking_the_world_temp_ha/sensor.py`
- Modify: `custom_components/linking_the_world_temp_ha/binary_sensor.py`
- Modify: `custom_components/linking_the_world_temp_ha/strings.json`
- Modify: `custom_components/linking_the_world_temp_ha/translations/en.json`
- Modify: `custom_components/linking_the_world_temp_ha/translations/zh-Hans.json`
- Create: `tests/native/test_diagnostics.py`

**Interfaces:**
- `HealthTracker.snapshot() -> dict[str, Any]` returns only scalar, list, and mapping data suitable for diagnostics.
- `build_anonymous_panel_map(records) -> dict[str, str]` assigns stable export-local labels `panel_01`, `panel_02`, and matching `room_01` labels in sorted identity order.
- Diagnostic sensors expose `ConnectionStage`, `FailureKind`, last command state, and panel count; binary connection is true only in READY.

- [ ] **Step 1: Write a failing secret-canary diagnostic test**

Configure unique canaries for host, username, password, client ID, total MAC, panel MAC, room name, public-key text, and a raw body. Serialize the complete diagnostic result to JSON and assert none of the canaries occurs.

- [ ] **Step 2: Write failing metric-content tests**

Produce reconnects, malformed input, invalid measurements, confirmed commands, one retry, and one timeout. Assert the anonymous snapshot contains all required counters, latency summary, stage/failure, queue sizes, and per-panel age/absence data.

- [ ] **Step 3: Replace direct hub object dumping**

Build diagnostics from sanitized entry configuration, `HealthTracker.snapshot()`, current system values without identities, and the anonymous panel mapping. Do not use `vars()` on protocol or hub objects.

- [ ] **Step 4: Update diagnostic entities and translations**

Show localized, stable state values rather than raw exception strings. Keep diagnostic entities always available so failures remain visible while the controller is offline.

- [ ] **Step 5: Verify diagnostics and logs**

Run: `pytest -q tests/native/test_diagnostics.py -vv`

Run: `rg -n 'body\.hex\(\)|frame\.body\.hex\(\)' custom_components/linking_the_world_temp_ha`

Expected: diagnostic tests PASS; any raw hex logging found is guarded by debug logging and never copied into health/diagnostics.

- [ ] **Step 6: Commit**

```bash
git add custom_components/linking_the_world_temp_ha tests/native/test_diagnostics.py
git commit -m "feat: add private runtime diagnostics"
```

---

### Task 8: Complete End-To-End HA Coverage And CI Gates

**Files:**
- Create: `tests/native/test_entities.py`
- Expand: `tests/native/test_setup.py`
- Expand: `tests/native/test_config_flow.py`
- Expand: `tests/native/test_connection_failures.py`
- Expand: `tests/native/test_repairs.py`
- Expand: `tests/native/test_diagnostics.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/validate-integration.yml`

**Interfaces:**
- No new production interface. This task locks the previously defined contracts through full real-HA behavior tests.

- [ ] **Step 1: Add failing end-to-end entity tests**

Cover system switch, mode and scene selects, humidifier availability, dynamic climate creation, temperature range/step, filtered sensors, ventilation/dehumidify panel-off constraints, command confirmation, rapid setpoint coalescing, retry, timeout recovery, and APP-originated push state synchronization.

- [ ] **Step 2: Add lifecycle and stress tests**

Run at least 100 fragmented/concatenated status frames and 50 rapid temperature requests with deterministic fake-server acknowledgements. Assert no parser desynchronization, stuck pending target, blocked later command, task leak, or false 100-degree/100-percent state.

- [ ] **Step 3: Add setup/unload/restart/uninstall tests**

Assert reload preserves identities, unload closes reader/background tasks, restart restores stored panels unavailable until reports arrive, and entry removal leaves no integration-owned tasks or issues.

- [ ] **Step 4: Run coverage and inspect missing lines**

Run: `pytest -q --cov=custom_components/linking_the_world_temp_ha --cov-report=term-missing --cov-fail-under=95`

Expected: PASS at 95% or higher. Add behavior-focused tests for any meaningful uncovered branch; exclude only defensive `TYPE_CHECKING` or platform-impossible lines with an explained pragma.

- [ ] **Step 5: Make CI enforce the stable gates**

The native job uses a `pytest_ha` matrix containing `0.13.354` (Home Assistant
`2026.8.0`) and `0.13.357` (Home Assistant `2026.8.3`), installs
`pytest-cov==6.2.1`, and runs the exact coverage command for both. Keep HACS and
hassfest jobs. When the supported current HA release changes, update only the
second matrix value and `requirements_test.txt`; retain `0.13.354` as the
minimum-version gate.

- [ ] **Step 6: Run the complete local verification set**

Run: `pytest -q --cov=custom_components/linking_the_world_temp_ha --cov-report=term-missing --cov-fail-under=95`

Run: `python -m compileall -q custom_components/linking_the_world_temp_ha`

Run: `python scripts/sync_addon_bridge.py --check`

Expected: all commands exit zero.

- [ ] **Step 7: Commit**

```bash
git add tests .github/workflows custom_components/linking_the_world_temp_ha
git commit -m "test: enforce native integration stable gates"
```

---

### Task 9: Document, Field-Verify, And Release 1.0.0

**Files:**
- Modify: `custom_components/linking_the_world_temp_ha/manifest.json`
- Modify: `hacs.json`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Create: `docs/TROUBLESHOOTING.md`
- Create: `docs/PRIVACY.md`
- Modify: `custom_components/linking_the_world_temp_ha/strings.json`
- Modify: `custom_components/linking_the_world_temp_ha/translations/en.json`
- Modify: `custom_components/linking_the_world_temp_ha/translations/zh-Hans.json`

**Interfaces:**
- Manifest version: `1.0.0`.
- HACS minimum Home Assistant: `2026.8.0`.
- Release tag: `v1.0.0` after merge to `main` and all remote checks pass.

- [ ] **Step 1: Update version metadata and release notes**

Set manifest to `1.0.0`, HACS baseline to `2026.8.0`, and add a `1.0.0` changelog section covering real HA tests, connection classification, reauth, Repairs, manual stale cleanup, diagnostics privacy, migration, and known firmware limitations.

- [ ] **Step 2: Update installation, troubleshooting, and privacy docs**

Document fresh HACS installation, 2026.8 baseline, admin/Test account separation, connection-state meanings, Repair workflows, 30-day observed-absence semantics, diagnostic redaction, recovery steps, and how to collect logs without raw protocol dumps.

- [ ] **Step 3: Run metadata and translation validation**

Run: `python -m json.tool custom_components/linking_the_world_temp_ha/manifest.json >/dev/null`

Run: `python -m json.tool custom_components/linking_the_world_temp_ha/translations/en.json >/dev/null`

Run: `python -m json.tool custom_components/linking_the_world_temp_ha/translations/zh-Hans.json >/dev/null`

Run: `test "$(python -c 'import json; print(json.load(open("custom_components/linking_the_world_temp_ha/manifest.json"))["version"])')" = 1.0.0`

Expected: all commands exit zero.

- [ ] **Step 4: Perform real HA 2026.8 field verification**

On the available HA instance, verify fresh install, configuration, restart, reload, high-frequency temperature control, APP-originated state push, controller disconnect/reconnect, a forced explicit-auth test hook or mocked setup reauth, Repair display/clear, diagnostic download, and uninstall. Record only sanitized outcomes in the PR description.

- [ ] **Step 5: Run final clean verification**

Run: `pytest -q --cov=custom_components/linking_the_world_temp_ha --cov-report=term-missing --cov-fail-under=95`

Run: `git diff --check origin/main...HEAD`

Run: `python scripts/sync_addon_bridge.py --check`

Expected: tests pass, coverage is at least 95%, no whitespace errors, and the preserved legacy add-on remains synchronized.

- [ ] **Step 6: Commit release preparation**

```bash
git add custom_components/linking_the_world_temp_ha hacs.json README.md CHANGELOG.md docs
git commit -m "release: prepare Linking The World Temp HA 1.0.0"
```

- [ ] **Step 7: Review, merge, and publish**

Push the feature branch, open a PR, wait for every required GitHub check, review the final diff against the specification, merge without rewriting unrelated history, then create GitHub release/tag `v1.0.0` from the merge commit. Do not publish if field verification or any gate remains incomplete.
