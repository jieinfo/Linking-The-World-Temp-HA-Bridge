# SDD ledger — plan: docs/superpowers/plans/2026-08-24-native-integration-1.0.md

## Baseline

- Worktree: `/private/tmp/linking-mode-constraint/.worktrees/native-integration-1.0`
- Branch: `feat/native-integration-1.0`
- Starting commit: `3f3899a`
- Python 3.12 baseline: 59 passed, 3 skipped because the real HA test runtime is not installed yet.
- Target/CI Python remains 3.13 through the Home Assistant 2026.8 test package.

## Pre-flight scan

| Scope | Producer / expectation | Consumer / implementation | Finding / ruling |
| --- | --- | --- | --- |
| Task 1 | Real HA fixtures and `FakeMC7021Server` | Tasks 2-8 integration tests | Clean; all later runtime tests consume the same fixture layer. |
| Task 1 / Task 8 | Current HA package `0.13.357` in local requirements | CI matrix also installs baseline `0.13.354` | Ruling: CI installs matrix package explicitly; local requirements remains current. |
| Task 2 | Typed protocol exceptions | Tasks 3-4 hub and config flow mappings | Clean; names and inheritance are fixed in Task 2. |
| Task 2 / Task 4 | Captured `0x02/0x05`, TLV `0x031c=1` rejection | `invalid_auth`, reauth, and retry pause | Clean; new packet evidence resolves the prior ambiguity. |
| Task 3 | `ConnectionStage`, `FailureKind`, `HealthTracker` | Tasks 4 and 7 issues/diagnostics | Clean; later tasks consume the declared enum values and snapshot API. |
| Task 3 / Task 5 | Hub owns lifecycle and runtime object | Panel registry moves persistence out of hub | Clean; Task 5 preserves hub entity-facing maps while changing storage ownership. |
| Task 3 / Task 7 | Health counters recorded at event sites | Diagnostics publishes sanitized snapshot | Clean; Task 7 must not recalculate protocol metrics. |
| Task 4 | `RepairManager` and connection issues | Task 6 extends same repairs module for stale panels | Clean; issue IDs are disjoint and flow dispatch keys are explicit. |
| Task 4 / Task 9 | Translation keys for auth/connection Repairs | Final documentation and validation | Clean; Task 9 validates rather than redesigns copy. |
| Task 5 | Store v2 migration and observed absence | Task 6 stale threshold and deletion | Clean; threshold ownership remains the panel registry. |
| Task 5 / Task 7 | Panel records contain private identity | Diagnostics maps records to anonymous labels | Clean; raw identity never leaves runtime/storage. |
| Task 5 / Task 8 | Dynamic discovery and restart behavior | End-to-end identity and stress tests | Clean; entity IDs remain unchanged by specification. |
| Task 6 | Entity/device/store cleanup | Task 8 uninstall and rediscovery tests | Clean; Task 8 broadens verification without adding deletion behavior. |
| Task 7 | Sanitized diagnostics and diagnostic entities | Task 8 coverage and Task 9 privacy docs | Clean. |
| Task 8 | 95% coverage and CI gates | Task 9 release gate | Clean; release cannot proceed on a failed matrix row. |
| Task 9 | Metadata `1.0.0`, HA `2026.8.0`, field verification | GitHub merge/release | Clean; merge/publish remains an external side effect requiring a final stop. |
| Task 1 self | Failing setup test, fixture implementation, baseline CI | Stated files contain all fixtures and workflow changes | Clean. |
| Task 2 self | Tests require explicit reject and conservative unknown handling | Protocol implementation waits for either login opcode | Clean. |
| Task 3 self | Runtime-data and state tests | Runtime/hub/platform files are all listed | Clean; use TYPE_CHECKING/string annotations to avoid runtime/hub import cycles. |
| Task 4 self | Config, reauth, and issue tests | Flow, hub, repairs, and translations listed | Clean; runtime rejection pauses retries until reload. |
| Task 5 self | Migration and injected-clock tests | Store model and hub integration listed | Clean. |
| Task 6 self | Threshold, cancellation, deletion, rediscovery tests | Repairs and registries listed | Clean; persisted record is deleted last. |
| Task 7 self | Secret canaries and metric assertions | Diagnostics, health, entities, translations listed | Clean. |
| Task 8 self | Entity, stress, lifecycle, and coverage gates | Tests and both CI workflows listed | Clean. |
| Task 9 self | Version, docs, validation, field test, publish | All metadata/docs listed | Clean; publish is deferred until explicit approval at the external-side-effect boundary. |

## Rulings

- Ruling: classify `(kind=0x02, opcode=0x05)` with TLV `0x031c=0x01` as explicit authentication rejection — packet evidence supplied 2026-08-24 proves the wrong-password path — risk if firmware differs is contained by requiring both the login-stage opcode and captured TLV.
- Ruling: pause reconnects after explicit rejection and wait for entry reload — avoids repeated known-invalid logins — cost is that a transient erroneous rejection requires user reauth/reload.
- Ruling: local baseline may run on bundled Python 3.12, while CI is authoritative for Python 3.13/HA 2026.8 — no local 3.13 runtime is installed — risk is caught by the required CI matrix before release.

## Packet evidence

- Reassembled stream `10.10.1.3:57978 -> 10.10.1.246:9000` decodes login request `(kind=2, opcode=4, sequence=1)` with username `admin` and the deliberately wrong password.
- Reverse stream decodes hello success `(1,3)` followed by login rejection `(2,5, sequence=0, body=1c03010001)` and TCP FIN.
- Production `YasHcpDecoder` produced these frames directly from the pcap payload, confirming the `0x02/0x05 + TLV 0x031c=1` rule independently of tcpdump formatting.

## Task 1 review

- Implementer: `01a0329c-e082-7723-a5f6-d556abc1e576`
- Commit: `264ddcd test: add real Home Assistant runtime`
- Review finding P1: reviewer claimed `0.13.357` is unpublished. Ruling: rejected — PyPI JSON returns the 0.13.357 wheel and source distribution; local pip hides it because the local interpreter is Python 3.12 while this release requires Python 3.13. GitHub CI explicitly uses Python 3.13, so the pin is runnable there.
- Review finding P3: delayed, closed, and concatenated fake-controller modes lack direct tests. Ruling: accept and fix in round 1 — these controls are shared infrastructure and need regression coverage before later tasks depend on them.
- Fix round 1 dispatched to the original implementer.
- Fix commit: `a69e375 test: cover fake controller behaviors`.
- Scoped re-review: all accepted findings addressed; no new breakage.
- Task 1: complete.

## Task 2 review

- Implementer: `01a032b2-0b7d-7531-b7d1-5ff203d5ad71`
- Commit: `02555a0 refactor: classify controller protocol failures`.
- Accepted High finding: queued kind-2 frames from before the current login request or a prior socket can be consumed as current credentials rejection.
- Accepted Medium finding: cancelling `_wait_for_opcodes` can leave helper tasks alive and consume a future frame.
- Accepted Low test gap: add pre-login, reused-client, cancellation, and wrong rejection-TLV value/length coverage.
- Ruling: use one inbox event stream containing frames plus a reader-stop sentinel, reset it for every socket, and discard/classify pre-login queued frames without ever treating them as current invalid credentials. This removes cancellation-prone helper tasks while preserving immediate EOF detection.
- Fix round 1 dispatched to the original implementer.
- Fix commit: `b4efafb fix: isolate controller connection sessions`.
- Verification: 16 focused connection tests, 22 protocol/framing tests, and 45 native tests passed.
- Scoped re-review: PASS; session isolation, cancellation cleanup, and conservative rejection-TLV coverage are all closed.
- Task 2: complete.

## Task 3 review

- Implementer: `01a032c8-f9bf-7f21-8081-a74f7c3a7624`.
- Commit: `cdd031f refactor: add typed runtime and connection health`.
- Accepted P1: platform forwarding failure/cancellation after hub start leaks the TCP runtime; setup must stop the hub before re-raising.
- Accepted P1: health failure messages can expose DNS hostnames and IPv6 addresses through diagnostics; sanitization must explicitly redact the configured host and generic endpoint forms.
- Accepted P2: pre-authentication connection attempts are incorrectly counted as disconnects because `_client` existence is not proof of a successful session.
- Accepted P2: `IncompatibleProtocol` does not increment the current handshake/login stage failure counter.
- Accepted P2: health snapshots shallow-copy history and allow callers to mutate retained dictionaries.
- Accepted test gaps: setup-forward failure/cancellation cleanup, hostname/IPv6 redaction, failed-attempt disconnect accounting, stage-specific protocol counters, and snapshot immutability need focused coverage.
- Fix round 1 dispatched.
- Fix commit: `0d42e93 fix: harden runtime health lifecycle`.
- Verification: 50 focused tests and 67 complete native tests passed; compile and diff checks passed.
- Scoped re-review: PASS; all five accepted findings and their regression coverage are closed.
- Task 3: complete.

## Task 4 review

- Implementer: `01a032ec-51d0-7143-8b00-0738f88b9768`.
- Commit: `d036e70 feat: add reauthentication and connection repairs`.
- Accepted P1: HA silently ignores `async_start_reauth()` while a conflicting entry flow is active, but the runner pauses unconditionally; cancellation of the conflicting flow can therefore leave neither reauth nor reconnect active.
- Accepted P2: reauth/reconfigure use `async_update_reload_and_abort()` while the entry also has an update listener, a combination deprecated by HA 2026.8; use the listener-compatible update flow.
- Accepted P2: config-entry removal does not delete entry-linked login/protocol issues, leaving orphan Repairs.
- Accepted P3: failed reauth input is not preserved in the returned form despite the flow contract and test requirement.
- Fix round 1 dispatched with real HA flow-progress and entry-removal regression coverage.
- Fix commit: `25262cd fix: harden reauthentication lifecycle`.
- Verification: 45 focused auth/Repair tests and 85 complete native tests passed.
- Scoped re-review: PASS; conflicting-flow reauth, listener-compatible updates, issue removal, and failed-form values are closed.
- Task 4: complete.

## Task 5 review

- Implementer: `01a03307-e122-7b10-88fc-4b2d894b603f`.
- Commit: `ad944be feat: track persistent panel lifecycle`.
- Accepted P1: observed absence uses UTC wall-clock deltas, allowing NTP/manual clock changes to manufacture or replay long absence intervals; in-process duration must use an injected monotonic clock.
- Accepted P1: controller silence is based on any TCP input, so non-status traffic can keep the session alive and a later status can backfill the whole stalled interval; valid-status continuity needs its own monotonic timeout and conservative restart.
- Accepted P2: a TLV stream with a valid prefix and truncated tail is accepted as valid status; whole-body TLV integrity must gate accounting and state mutation.
- Accepted P2: short-term offline expiry updates only the hub thermostat mirror, leaving persisted `PanelRecord.available` true.
- Fix round 1 dispatched with clock-jump, status-gap, truncated-TLV, and mirror-consistency regression tests.
- Fix commit: `ee51966 fix: harden observed panel lifecycle`.
- Verification: 10 focused Task 5 tests and 100 complete native tests passed.
- Scoped re-review: PASS; monotonic duration, status continuity, whole-body TLV validation, and availability persistence are closed.
- Task 5: complete.
- Task 6: complete.
  - Added exact 30-day stale-panel Repairs with explicit confirmation and
    report-driven automatic recovery.
  - Safe cleanup selects only this config entry's stable panel entity prefix,
    retains devices with unrelated references, deletes persisted source state
    last, and reloads a loaded entry so rediscovery remains duplicate-free.
  - Entry removal now also clears stale-panel Repairs; diagnostics redact
    identities to anonymous panel/room labels.
- Fix round 1: observed absence now uses an injected in-process monotonic
  clock; persisted UTC checkpoints are display-only and cannot backfill after a
  restart or wall-clock correction. The hub tracks last valid status separately
  from arbitrary TCP traffic, pauses on status gaps, and the registry repeats
  the monotonic continuity defense. Whole-body TLV validation blocks partial
  updates from truncated tails. Short-term panel expiry now synchronizes and
  persists `PanelRecord.available`; fresh reports restore it.
- Fix verification: 10 focused Task-5 tests and 100 native tests passed;
  compileall and diff checks passed.
- Task 5 review fix round 1: complete; scoped re-review remains pending.

## Task 3 implementation

- Added typed `LinkingTempRuntime` config-entry data and the declared
  connection/failure enums.
- Added bounded, sanitized health counters/history and wired protocol lifecycle,
  decoder, command, measurement, and disconnect events to it.
- Replaced all platform and diagnostics `hass.data` hub lookups with
  `entry.runtime_data.hub`.
- Verified that controller availability remains false until both `READY` and a
  valid total-controller status report; stage sequence is
  CONNECTING -> HANDSHAKING -> AUTHENTICATING -> READY.
- A direct early-stop test was deliberately removed after deterministic root
  cause tracing: pytest-homeassistant-custom-component's cleanup hook deadlocks
  by observing its own async fixture finalizer, despite no Linking Temp task
  remaining. The normal fixture unload path is clean and remains the lifecycle
  gate.
- Verification: 32 targeted tests and 49 native tests passed; compile and diff
  checks passed; no pytest process remained.
- Commit: `cdd031f refactor: add typed runtime and connection health`.
- Task 3: complete.
