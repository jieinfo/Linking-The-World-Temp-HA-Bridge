# Linking The World Temp HA 1.0 Design

## Purpose

Prepare the native HACS integration for a `1.0.0` stable release by adding
real Home Assistant runtime tests, explicit authentication and connection
failure handling, actionable Repairs, deliberate stale-panel management, and
privacy-preserving diagnostics. Existing controller behavior, entity IDs,
device identifiers, dynamic panel discovery, operating-mode constraints, and
command semantics must remain compatible.

This remains a community HACS integration. The release criteria in this
document are project guarantees, not a claim of an official Home Assistant
Integration Quality Scale tier.

## Platform Baseline

- Minimum supported Home Assistant version: `2026.8.0`.
- The integration remains local push and uses one shared MC7021 TCP session per
  config entry.
- The implementation must not create MQTT entities or require the legacy
  bridge add-on.
- Existing config entries and stored panel data must migrate without user
  intervention.
- A `1.0.0` upgrade must not duplicate devices, entities, or panel records.

## Component Boundaries

### Protocol client

`protocol.py` owns TCP framing, stream resynchronization, handshake, login,
initial queries, and decoded controller messages. It exposes typed failures for
these stages:

- TCP connection refused or timed out
- handshake timed out or returned an incompatible response
- login timed out
- explicit authentication rejection
- malformed or unsupported protocol response

The protocol client must not create Home Assistant issues or mutate entity and
device registries.

### Hub

`hub.py` orchestrates the connection lifecycle, reconnect backoff, command
queue, confirmation matching, state distribution, and entity notifications. It
uses this explicit lifecycle:

`DISCONNECTED -> CONNECTING -> HANDSHAKING -> AUTHENTICATING -> READY`

The connection sensor reports connected only in `READY`. A successful TCP
socket alone is never reported as an authenticated controller connection.

The hub is stored on a typed `ConfigEntry.runtime_data` object. Platforms use
that shared runtime object instead of looking up the hub in `hass.data`.

### Panel registry

`panel_registry.py` owns dynamic panel discovery, persisted panel metadata,
monitored absence accounting, and confirmed panel deletion. It does not parse
raw protocol frames or send controller commands.

### Health metrics

`health.py` owns bounded counters, timestamps, latency summaries, the current
failure classification, and the privacy-safe diagnostic snapshot. Raw frames,
credentials, keys, and tokens are never stored in health history.

### Repairs

`repairs.py` maps actionable runtime conditions to Home Assistant issues and
clears them after recovery. Repair flows may update credentials or explicitly
delete one stale panel. Non-actionable transient failures remain log and
diagnostic events and do not create issue spam.

## Authentication And Failure Handling

An explicit negative authentication response raises the integration's typed
authentication exception. During config entry setup or an established runtime
session, that exception is translated to Home Assistant's authentication
failure mechanism and starts a reauthentication flow. The reauthentication
flow asks for username and password, validates them against the configured
controller, updates the existing entry, and reloads it. It must not create a
second config entry.

Only a rejection marker whose meaning is established by protocol evidence may
be classified as explicit authentication rejection. An unknown response,
missing response, closed socket, or inferred field value must not be guessed to
mean invalid credentials.

A login-stage timeout is ambiguous because the available captures do not prove
that an incorrect password always produces a distinct negative reply. It must
therefore not be treated as invalid authentication. Three consecutive login
stage timeouts create one Repair that asks the user to verify controller
availability and credentials. A later successful login clears the issue and
resets the consecutive-failure counter.

TCP failures and handshake failures are tracked separately. Repeated protocol
incompatibility creates an actionable Repair that identifies a likely
unsupported firmware or protocol response. Network failures continue to use
bounded reconnect backoff and diagnostics without repeatedly creating issues.

All expected setup errors produce localized, user-readable config-flow errors.
Raw Python tracebacks remain available only for unexpected defects in logs.

## Stale Panel Lifecycle

Each discovered panel persists these fields:

- stable panel identifier and existing Home Assistant unique ID
- latest known room name
- first discovery time in UTC
- last valid report time in UTC
- current availability
- accumulated monitored absence seconds
- timestamp of the last absence-accounting checkpoint

Absence time accumulates only while all of the following are true:

1. The controller session is in `READY`.
2. The integration is receiving valid controller status traffic, proving that
   the status stream is alive.
3. The panel has not produced a new valid panel report during the accounting
   interval.

HA shutdown, controller disconnection, failed login, handshake failure, and a
stalled or malformed status stream pause absence accounting. A controller
reconnect does not erase an already accumulated absence duration. A new valid
report from that panel immediately marks it online, resets accumulated absence
to zero, updates its room metadata, and clears its stale-panel Repair.

After 30 accumulated days, the integration creates one Repair for that panel.
No panel is ever deleted automatically. The Repair displays the latest room
name, a shortened privacy-safe MAC representation, and the last valid report
time. Ignoring or cancelling the Repair does not change stored data.

When the user explicitly confirms deletion, the integration removes:

1. entity registry entries owned only by the panel;
2. the panel's device registry entry when no unrelated entities remain;
3. the panel's persisted record; and
4. the associated Repair.

If the physical panel later reports again, normal discovery creates it as a
newly discovered panel using the same stable protocol identity. HA may restore
user customizations according to its own registry behavior, but the integration
must not manufacture a duplicate record.

## Storage And Migration

The panel store receives a versioned schema upgrade. Migration preserves all
existing panel identifiers, room names, availability data, and entity/device
identity. Missing new timestamps are initialized conservatively: migrated
panels start with zero monitored absence, so upgrading cannot immediately
produce stale-panel Repairs.

Wall-clock timestamps use timezone-aware UTC values. Durations used inside one
running process use monotonic time. Persisted absence accounting is committed
periodically and on clean unload without writing on every status frame.

## Diagnostics And Privacy

Diagnostics include:

- lifecycle stage and sanitized current failure category
- connection attempts, successful sessions, reconnects, and disconnects
- handshake and authentication success/failure counts
- decoded frames, malformed frames, discarded bytes, and resynchronizations
- invalid measurement and ignored status counts
- commands sent, confirmed, retried, coalesced, blocked, and timed out
- command confirmation latency summary
- pending and queued command counts
- number of panels and per-panel availability, report age, and monitored
  absence duration
- relevant non-secret configuration values and integration version

Diagnostics redact the controller host, username, password, client ID,
controller MAC, panel MAC addresses, room names, public/private keys, tokens,
and raw frames. Panels and rooms receive deterministic labels such as
`panel_01` and `room_01` within one diagnostic export so relationships remain
debuggable without exposing household data.

## Real Home Assistant Test Strategy

Tests use `pytest-homeassistant-custom-component` and a real Home Assistant test
instance rather than hand-written HA stubs. A programmable asynchronous fake
MC7021 server covers:

- fragmented, coalesced, malformed, and resynchronized TCP frames
- successful and timed-out handshake and login stages
- explicit authentication rejection
- controller state flow and dynamic panel discovery
- disconnect, reconnect, and stalled status streams
- delayed, missing, and out-of-order command confirmations

Test suites cover:

- initial config flow, duplicate prevention, reconfigure, options, and reauth
- config entry setup, unload, reload, and Home Assistant restart restoration
- entity creation, stable identity, availability, control, and mode constraints
- rapid temperature changes, command coalescing, retry, timeout, and recovery
- each Repair's creation, deduplication, recovery clearing, and user flow
- stale-panel accounting pauses, 30-day threshold, cancellation, deletion, and
  rediscovery
- storage migration and registry cleanup
- diagnostic content, metrics, and absence of every sensitive field

Coverage for `custom_components/linking_the_world_temp_ha` must be at least
95%. CI runs tests against the `2026.8` baseline and the current supported Home
Assistant release, plus HACS validation and hassfest. If the current release is
also `2026.8`, one runtime target is sufficient until a newer release exists.

## Logging And User Experience

Production logs contain lifecycle transitions and concise recovery guidance.
Protocol byte dumps are debug-only. Command timeout messages include the
anonymized target, command type, expected state, timeout, retry count, and queue
state. Repeating failures are rate-limited so a disconnected controller cannot
flood logs or Repairs.

All new Repair, config-flow, diagnostic entity, and error messages are provided
in English and Simplified Chinese. User-facing text distinguishes controller
offline, authenticating, authentication rejected, protocol incompatible, and
ready states.

## Release Acceptance

`1.0.0` can be released only when:

1. All real HA tests, coverage checks, HACS validation, and hassfest pass.
2. Installation, configuration, reload, restart, reauth, Repair handling, and
   uninstall are exercised on a real Home Assistant `2026.8` environment.
3. Existing config entries and panel stores migrate without duplicate devices
   or lost user-visible customizations.
4. Captured protocol playback and rapid temperature-command stress tests pass
   without permanent parser loss, queue deadlock, or stuck confirmation state.
5. Diagnostic exports contain no configured secrets, addresses, real MACs,
   room names, keys, tokens, or raw protocol frames.
6. README, troubleshooting, privacy documentation, translations, and
   `CHANGELOG.md` describe the stable behavior and supported HA baseline.
7. The manifest, release notes, and Git tag all use version `1.0.0`.

These gates qualify the project for its own stable release designation. They do
not guarantee compatibility with unknown controller firmware or replace field
testing across several households.
