# Offline Synchronization: Device Identity, Causal Ordering & Sync Protocol

> **Status**: Design — pending review before implementation.
> **Depends on**: `docs/clinical-event-invariants-and-metadata.md`, `docs/aggregates-streams-consistency.md`, `docs/event-processing-pipeline.md`
> **Governing rules**: `.aas/architecture-rules.yaml`

---
---

# Part 1 — Device Identity Model

## What Is a Device?

A **device** is any computing endpoint that produces clinical events — a tablet in an exam room, a laptop on a nurse's cart, a desktop at the front desk, a phone running the mobile app. Each device operates as an independent event-producing node.

A device is NOT a user. A single clinician may use multiple devices during a shift. A single device may be used by multiple clinicians (shared workstation). Device identity tracks the **machine**, not the person (the person is tracked by `performedBy`).

---

## Device Identity Structure

Every device in the system is assigned a **Device Identity Record** at provisioning time:

```
DeviceIdentity:
  device_id:        string    — globally unique device identifier (UUID v4)
  device_name:      string    — human-readable label ("Exam Room 3 Tablet")
  organization_id:  UUID      — the organization this device belongs to
  facility_id:      UUID      — the default facility (may be overridden per event)
  provisioned_at:   datetime  — when this device was registered
  device_type:      enum      — tablet | laptop | desktop | mobile | kiosk
  clock_drift_ms:   integer   — last known clock drift relative to server (0 if unknown)
```

### Identity Properties

| Property | Guarantee |
|----------|-----------|
| **Globally unique** | `device_id` is a UUID v4, generated at provisioning. No two devices share an ID, even across organizations. |
| **Stable** | The `device_id` never changes for the lifetime of the device. Replacing hardware = new device ID. |
| **Self-contained** | The device knows its own identity offline. It does not need to contact a server to learn its ID. |
| **Org-bound** | A device belongs to exactly one organization. It cannot produce events for a different organization. |

---

## Why Device Identity Is Required

### 1. Global Event Uniqueness

Every event has an `eventId` (UUID v7). UUID v7 embeds a millisecond-precision timestamp and random bits. But if two devices have identical clocks and generate events in the same millisecond, the random bits alone provide collision resistance. Adding `device_id` to event metadata provides a **secondary uniqueness axis** — even in the astronomically unlikely case of UUID collision, the device origin disambiguates.

More practically: `device_id` enables the system to **partition event generation** conceptually. Each device generates its own sequence of events. No two devices share a sequence.

### 2. Conflict Attribution

When a version conflict is detected during sync (two devices wrote to the same lifecycle aggregate), the system must know which device produced each conflicting event. Without `device_id`, the conflict resolver cannot:
- Present meaningful information to the clinical reviewer ("Exam Room 3 Tablet recorded triage at 09:05, Front Desk Desktop recorded encounter began at 09:04")
- Apply device-specific resolution policies
- Quarantine events from a malfunctioning device

### 3. Offline Event Provenance

Events recorded offline may arrive at the central store minutes, hours, or (in disaster scenarios) days after `occurredAt`. When they arrive, the system must know:
- **Where** they came from (which device)
- **Whether that device is trusted** (provisioned, not revoked)
- **What the device's clock state was** (to assess timestamp reliability)

### 4. Security & Quarantine

If a device is lost, stolen, or compromised:
- All events from that `device_id` after a certain timestamp can be flagged for review.
- The device can be revoked — future sync attempts are rejected.
- Historical events from the device remain in the store (clinical data is sacred) but are annotated.

### 5. Sync State Tracking

Each device maintains its own sync cursor — the last event it successfully uploaded to the central store. The sync protocol tracks these cursors per `device_id`:
```
SyncCursor:
  device_id:         string
  last_synced_event:  UUID     — event_id of the last successfully synced event
  last_synced_at:     datetime — when the last sync completed
  pending_count:      integer  — events waiting to be synced
```

---

## Metadata Added to Events

The existing event metadata already includes `device_id` and `connection_status` (defined in `docs/clinical-event-invariants-and-metadata.md`). The device identity model adds **three additional fields** to the event envelope:

| Field | Type | Set By | Purpose |
|-------|------|--------|---------|
| `deviceId` | string | device | **Already exists.** Identifies the originating device. |
| `connectionStatus` | enum | device | **Already exists.** Online or offline at recording time. |
| `deviceClockDriftMs` | integer | device | **New.** Estimated clock drift at event creation time (milliseconds, positive = device ahead of server). Enables the sync protocol to assess timestamp reliability. |
| `localSequenceNumber` | integer | device | **New.** Monotonically increasing counter per device. Each device maintains its own counter starting at 1. This provides a **total order of events per device** independent of wall-clock time. |
| `syncBatchId` | UUID | sync protocol | **New.** Set during sync — groups events that arrived in the same sync batch. Null for events created online and persisted directly. Used for sync debugging and audit. |

### Why `localSequenceNumber`?

System clocks cannot be trusted (device clocks drift, users change time zones, NTP fails offline). But the order in which a single device produced events is known — the device was there. `localSequenceNumber` captures this:

```
Device A (offline):
  LSN 1: VitalSignsRecorded       (occurredAt: 09:08)
  LSN 2: SymptomReported           (occurredAt: 09:10)
  LSN 3: ExaminationFindingNoted   (occurredAt: 09:12)
```

Even if the device clock is wrong, LSN 1 < LSN 2 < LSN 3 is **guaranteed** by the device. This is a critical input for causal ordering (Part 2).

### Why `deviceClockDriftMs`?

When a device syncs, the server compares the device's reported time against server time and records the drift. This drift estimate is stored on the device and stamped onto every subsequent event. During causal ordering, events with large drift values receive lower confidence for their `occurredAt` timestamps.

---

## How Device Identity Supports Offline Work

### Independent Event Production

Each device produces events locally without waiting for network. Events are written to the device's local event store immediately. The user never sees a spinner or a "no connection" error for clinical workflows.

```
Device A (offline):                    Device B (offline):
  Local store:                           Local store:
    VitalSigns-{uuid-1} v1                Diagnosis-{uuid-2} v1
    Symptom-{uuid-3} v1                   ClinicalNote-{uuid-4} v1
  LSN: 47                                LSN: 112
  Pending sync: 2 events                 Pending sync: 2 events
```

### Per-Device Outbox

Each device maintains an **outbox** — a queue of events that have been persisted locally but not yet synced to the central store. The outbox is ordered by `localSequenceNumber`:

```
Outbox (Device A):
  [LSN 46] → event_id: abc-123, type: VitalSignsRecorded
  [LSN 47] → event_id: def-456, type: SymptomReported
```

On sync, the outbox is drained from oldest to newest. Successfully synced events are removed from the outbox.

### Device Revocation

If a device is compromised:
```
1. Admin marks device_id as REVOKED in the central authority
2. Next sync attempt from that device is rejected
3. All events from that device_id after revocation_timestamp are flagged
4. Clinical review task created for flagged events
5. Historical events remain (clinical data is sacred)
```

---
---

# Part 2 — Causal Ordering Strategy

## The Problem

System clocks cannot be trusted:
- Device clocks drift (up to seconds per day without NTP).
- Users change time zones manually.
- NTP is unavailable during offline operation.
- A device's battery dying and restarting can reset its clock.

Yet projections and clinical record views need a **deterministic, meaningful order** for events. "When did the doctor begin the encounter relative to the nurse's vitals?" is a clinical question that affects the medical record.

## The Solution: Hybrid Causal Clock

The system uses a **hybrid ordering model** that combines multiple signals into a single deterministic order. No single signal is trusted alone.

### Ordering Signals (Priority Order)

| Priority | Signal | Source | Trust Level | What It Captures |
|----------|--------|--------|-------------|-----------------|
| 1 | **Aggregate version** | Event store | Absolute (per-stream) | Total order within one aggregate stream. |
| 2 | **Causation chain** | `causationId` | Absolute | Causal parent → child relationship. A child always happened after its parent. |
| 3 | **Local sequence number** | Device | Absolute (per-device) | Total order of all events produced by one device. |
| 4 | **Occurred-at timestamp** | Performer | Estimated | The clinician's claim of when it happened. Subject to clock drift. |
| 5 | **Recorded-at timestamp** | Device | Estimated | When the device persisted the event. Subject to clock drift but not to human error. |
| 6 | **Event ID (UUID v7)** | System | Deterministic tiebreaker | Time-sortable UUID. Breaks ties when all other signals are equal. |

### Ordering Rules

Given two events E₁ and E₂, their order is determined by the first rule that distinguishes them:

**Rule 1: Same aggregate stream → use aggregate version.**
```
if E₁.aggregateId == E₂.aggregateId:
    order by aggregateVersion (ascending)
    # This is absolute — the event store enforces contiguous versioning.
```

**Rule 2: Causal dependency → parent before child.**
```
if E₂.causationId == E₁.eventId:
    E₁ < E₂  # E₁ caused E₂, so E₁ is earlier.
if E₁.causationId == E₂.eventId:
    E₂ < E₁
```

**Rule 3: Same device → use local sequence number.**
```
if E₁.deviceId == E₂.deviceId:
    order by localSequenceNumber (ascending)
    # The device produced them in this order — guaranteed.
```

**Rule 4: Different devices → use adjusted occurred-at.**
```
adjusted_time(E) = E.occurredAt - E.deviceClockDriftMs
order by adjusted_time (ascending)
# Compensate for known clock drift.
```

**Rule 5: Adjusted times are equal → use recorded-at.**
```
order by recordedAt (ascending)
```

**Rule 6: Everything equal → use event ID.**
```
order by eventId (ascending)
# UUID v7 is time-sortable. This is the deterministic tiebreaker.
```

### Why This Ordering Is Deterministic

Given the same set of events, any node applying these rules produces the **same order**. No rule depends on:
- The current time
- The order events arrived at the node
- The node's own clock
- External state

All inputs are embedded in the events themselves. This means:
- Two different servers ordering the same events produce the same result.
- A projection rebuilt from events produces the same timeline.
- A device syncing events and a server receiving them agree on order.

### Why This Ordering Survives Offline Work

Each priority level handles a specific offline scenario:

| Scenario | Which rule resolves it |
|----------|----------------------|
| Two events in the same aggregate, one offline | Rule 1 — aggregate version is always sequential |
| An event that triggered a downstream event on another device | Rule 2 — causation chain is preserved in metadata |
| A nurse records vitals then symptoms on the same tablet | Rule 3 — local sequence number captures device-local order |
| Two doctors record diagnoses on different tablets at "the same time" | Rule 4 — adjusted occurred-at, compensated for drift |
| Two events with identical timestamps from different devices | Rule 5/6 — recorded-at or UUID tiebreaker |

### Causal Ordering Is Not Global Ordering

This system provides **causal ordering**, not total global ordering. Two events from different devices, in different aggregate streams, with no causal relationship may be ordered differently depending on clock drift. This is acceptable because:

1. **No invariant requires global ordering.** All invariants are per-aggregate (strong consistency) or per-relationship (eventual consistency via projections).
2. **Clinical workflows are inherently concurrent.** When Dr. A and Nurse B are working simultaneously, asking "which happened first?" is often meaningless — both are independently valid.
3. **Where order matters, it's captured explicitly** — by aggregate version (same stream) or causation chain (cross-stream dependency).

---

## Causal Ordering Diagram

```
Device A (Nurse Tablet)              Device B (Doctor Laptop)
  LSN 1: VitalSigns v1                LSN 1: DiagnosisMade v1
  LSN 2: Symptom v1                   LSN 2: TreatmentPlan v1
  LSN 3: ExamFinding v1               LSN 3: ClinicalNote v1

Ordering within Device A: LSN 1 < LSN 2 < LSN 3 (Rule 3, absolute)
Ordering within Device B: LSN 1 < LSN 2 < LSN 3 (Rule 3, absolute)

Cross-device ordering: adjusted occurredAt (Rule 4, estimated)
  A.LSN1 (09:08) vs B.LSN1 (09:10) → A.LSN1 first (if clocks agree)

If DiagnosisMade.causationId = VitalSigns.eventId:
  VitalSigns < DiagnosisMade (Rule 2, absolute — causation overrides clocks)
```

---
---

# Part 3 — Distributed Event Synchronization Protocol

## Design Principles

1. **Nodes exchange missing events.** Sync transfers events the other side doesn't have — nothing more.
2. **Events are append-only.** Sync never modifies, reorders, or deletes existing events.
3. **No event modification allowed.** An event arriving during sync is identical to one created locally.
4. **Sync must be idempotent.** Running the same sync twice produces the same result as running it once.

---

## Sync Topology

The system uses a **hub-and-spoke** topology:
- **Hub**: Central server (or cluster) holding the authoritative event store.
- **Spokes**: Devices with local event stores.

Devices sync with the hub. Devices do not sync directly with each other. This simplifies conflict detection and prevents event divergence between devices.

```
                    ┌─────────────┐
                    │   Central   │
                    │    Hub      │
                    │ (authority) │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
        ┌─────▼─────┐ ┌───▼───┐ ┌─────▼─────┐
        │ Device A  │ │ Dev B │ │ Device C  │
        │ (tablet)  │ │(laptop│ │ (desktop) │
        └───────────┘ └───────┘ └───────────┘
```

---

## Sync Protocol: Four Phases

### Phase 1: Handshake

The device initiates sync by sending its identity and sync state to the hub.

```
DEVICE → HUB: SyncHandshake
  device_id:           "device-a-uuid"
  organization_id:     "org-uuid"
  last_hub_event_id:   "uuid-of-last-event-received-from-hub"
  last_hub_event_lsn:  42            — hub's global position last seen
  device_lsn:          47            — device's current local sequence number
  pending_count:       5             — events in outbox
  device_clock:        "2026-02-14T18:30:00Z"  — device's current time
  protocol_version:    1
```

The hub validates:
1. **Device exists and is not revoked.** If revoked → reject sync, return `DEVICE_REVOKED`.
2. **Organization matches.** If mismatch → reject sync, return `ORG_MISMATCH`.
3. **Protocol version is supported.** If unsupported → reject sync, return `PROTOCOL_UNSUPPORTED`.
4. **Clock drift.** Hub compares `device_clock` against server time, computes drift, stores it.

```
HUB → DEVICE: SyncHandshakeAck
  status:              "READY"
  hub_clock:           "2026-02-14T18:30:02Z"
  clock_drift_ms:      -2000          — device is 2 seconds behind
  hub_current_lsn:     156            — hub's current global position
  events_available:    114            — events the hub has that the device hasn't seen
```

The device stores `clock_drift_ms` for stamping future events.

---

### Phase 2: Missing Event Detection

Both sides now know what the other is missing.

#### Hub detects what device is missing:
```
hub_events_for_device = hub.events_after(device.last_hub_event_lsn)
  filtered by: organization_id = device.organization_id
```

The hub doesn't send the device events from other organizations. It also filters by visibility if the device has restricted access.

#### Device declares what hub is missing:
The device's outbox contains events persisted locally but not yet on the hub.

```
device_outbox = device.events_after(device.last_synced_event_id)
  ordered by: localSequenceNumber (ascending)
```

No complex diff algorithm is needed. The outbox is the single source of truth for "what hasn't been synced yet."

---

### Phase 3: Event Transfer

Transfer happens in two directions, sequentially.

#### Step 3a: Device → Hub (Upload)

The device sends its outbox events to the hub, in `localSequenceNumber` order:

```
DEVICE → HUB: SyncUpload
  sync_batch_id:  "batch-uuid"      — unique ID for this sync batch
  events: [
    { event (full DomainEvent envelope, LSN 43) },
    { event (full DomainEvent envelope, LSN 44) },
    { event (full DomainEvent envelope, LSN 45) },
    { event (full DomainEvent envelope, LSN 46) },
    { event (full DomainEvent envelope, LSN 47) },
  ]
```

For each event, the hub:

1. **Checks `eventId` for deduplication.** If event already exists → skip (idempotent).
2. **Checks aggregate version.** If this creates a new stream → accept. If version matches expected → accept. If version conflicts → apply conflict resolution (per `aggregates-streams-consistency.md`).
3. **Sets `syncBatchId`** on the event for audit.
4. **Stamps `recordedAt`** if not already set (online events already have it; offline events get it now).
5. **Checks cross-aggregate invariants** (eventually consistent). If violated → accept event + emit `CompensationRequired` (clinical data is sacred).

```
HUB → DEVICE: SyncUploadAck
  accepted:   [event_id_1, event_id_2, event_id_3, event_id_5]
  duplicate:  [event_id_4]           — already existed, no-op
  conflicted: []                     — none in this batch
  compensations: []                  — no invariant violations detected
```

The device removes accepted and duplicate events from its outbox.

#### Step 3b: Hub → Device (Download)

The hub sends events the device hasn't seen:

```
HUB → DEVICE: SyncDownload
  events: [
    { event (from other devices, LSN 43-156) },
    ...
  ]
  hub_current_lsn: 161               — updated after upload
```

Events are sent in hub insertion order (which respects per-aggregate versioning). The device:

1. **Checks `eventId` for deduplication.** If event already exists locally → skip.
2. **Appends to local event store.** No version checking needed — the hub has already validated ordering.
3. **Updates local projections.** Downloaded events are dispatched through the local event dispatcher.

```
DEVICE → HUB: SyncDownloadAck
  received_count: 114
  last_hub_event_id: "uuid-of-last-received"
  last_hub_event_lsn: 161
```

---

### Phase 4: Duplicate Prevention

Duplicate prevention operates at three levels:

#### Level 1: Event ID Deduplication (Primary)

Every event has a globally unique `eventId` (UUID v7). Both the hub and device check `eventId` before accepting:

```
if event_store.event_exists(event.event_id):
    return DUPLICATE  # skip, no error
```

This is the **primary** duplicate prevention mechanism. It is sufficient on its own — the other levels are defense-in-depth.

#### Level 2: Outbox Tracking (Device-Side)

The device's outbox tracks which events have been successfully synced. After `SyncUploadAck`:

```
for event_id in ack.accepted + ack.duplicate:
    outbox.remove(event_id)
```

An event is only removed from the outbox after the hub acknowledges it. If sync is interrupted (network drops mid-transfer), the outbox retains the events, and the next sync re-sends them. The hub's event ID dedup handles the overlap.

#### Level 3: Sync Cursor (Hub-Side)

The hub tracks each device's sync state:

```
DeviceSyncState:
  device_id:            "device-a-uuid"
  last_uploaded_lsn:    47            — device's LSN of last uploaded event
  last_downloaded_lsn:  161           — hub's LSN of last downloaded event
  last_sync_at:         "2026-02-14T18:30:05Z"
```

On the next sync, the hub knows:
- "Device A has seen hub events up to LSN 161" → only send events after 161.
- "Device A has uploaded through device LSN 47" → if the device re-sends LSN 43-47, they're duplicates.

---

## Idempotency Proof

Running the same sync operation twice produces the same result:

```
Sync attempt 1:
  Upload: events [A, B, C] → hub accepts [A, B, C]
  Download: events [X, Y] → device accepts [X, Y]

Sync attempt 2 (identical):
  Upload: events [A, B, C] → hub returns duplicate [A, B, C] (already exist)
  Download: events [X, Y] → device returns duplicate [X, Y] (already exist)

Result: identical event stores after attempt 1 and attempt 2.
```

This holds because:
1. **Event append is idempotent** — duplicate `eventId` is a no-op (event store invariant).
2. **Outbox drain is idempotent** — removing an already-removed event is a no-op.
3. **Projection updates are idempotent** — duplicate `eventId` is skipped (projection handler invariant).
4. **Sync cursors are monotonic** — they only advance, never regress.

---

## Sync Failure Recovery

| Failure | When | Recovery |
|---------|------|----------|
| **Network drops during handshake** | Phase 1 | Device retries handshake. No state changed. |
| **Network drops during upload** | Phase 3a | Outbox retains unsent events. Next sync re-sends. Hub deduplicates already-received events. |
| **Network drops during download** | Phase 3b | Device retains old cursor. Next sync re-downloads. Device deduplicates already-received events. |
| **Hub rejects device (revoked)** | Phase 1 | Device shows "Contact administrator" error. No events lost — they're in the local store. |
| **Hub crashes mid-sync** | Any phase | Hub state is transactional — partially received uploads are rolled back. Next sync starts clean. |
| **Device crashes mid-sync** | Any phase | Outbox and cursor are persistent. On restart, sync resumes from last known state. |

---

## Sync Protocol Diagram

```
DEVICE                                          HUB
  │                                               │
  │─── SyncHandshake ──────────────────────────►  │
  │    (device_id, last_hub_lsn, pending_count)   │
  │                                               │
  │  ◄─── SyncHandshakeAck ──────────────────────│
  │       (status, clock_drift, events_available) │
  │                                               │
  │─── SyncUpload ────────────────────────────►   │
  │    (batch_id, events[])                       │
  │                                               │
  │               ┌──────────────────────────┐    │
  │               │ For each event:          │    │
  │               │   dedup by eventId       │    │
  │               │   check aggregate version│    │
  │               │   resolve conflicts      │    │
  │               │   check invariants       │    │
  │               │   persist                │    │
  │               └──────────────────────────┘    │
  │                                               │
  │  ◄─── SyncUploadAck ─────────────────────────│
  │       (accepted[], duplicate[], conflicts[])  │
  │                                               │
  │  ◄─── SyncDownload ──────────────────────────│
  │       (events[], hub_current_lsn)             │
  │                                               │
  │               ┌──────────────────────────┐    │
  │               │ For each event:          │    │
  │               │   dedup by eventId       │    │
  │               │   append to local store  │    │
  │               │   dispatch to projections│    │
  │               └──────────────────────────┘    │
  │                                               │
  │─── SyncDownloadAck ───────────────────────►   │
  │    (received_count, last_hub_lsn)             │
  │                                               │
  │              SYNC COMPLETE                    │
```

---

## Sync Properties Summary

| Property | Guarantee |
|----------|-----------|
| **Idempotent** | Running sync N times produces the same result as running it once. |
| **Append-only** | Sync never modifies or deletes existing events on either side. |
| **Conflict-safe** | Conflicts are detected per-aggregate and resolved per the consistency model. Clinical data is never discarded. |
| **Order-preserving** | Events are uploaded in device LSN order and downloaded in hub insertion order. Per-aggregate versioning is maintained. |
| **Resumable** | Interrupted syncs resume from the last acknowledged point. No data is lost. |
| **Auditable** | Every synced event carries `syncBatchId`, enabling tracing of when and how events arrived. |
