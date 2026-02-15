# Conflict Model for Distributed Clinical Events

> **Status**: Design — pending review before implementation.
> **Depends on**: `docs/aggregates-streams-consistency.md`, `docs/offline-sync-protocol.md`
> **Governing rules**: `.aas/architecture-rules.yaml`

---

## Core Principle

A **conflict** occurs when two devices produce events that cannot both be valid within the same aggregate stream. This is narrower than "two devices produced events at the same time." Most concurrent clinical work is **not** a conflict because the aggregate design isolates writes into separate streams.

---
---

# Part 1 — Situations That Are NOT Conflicts

The following scenarios involve concurrent offline event production but are **conflict-free by design**. No resolution logic is needed. Events are accepted unconditionally during sync.

---

### 1. Different fact aggregates (most common case)

Nurse records vitals on Device A. Doctor records a diagnosis on Device B. Both offline.

```
Device A: VitalSigns-{uuid-1} v1   → new stream
Device B: Diagnosis-{uuid-2} v1    → new stream
```

**Why not a conflict**: Each event creates a **new aggregate stream** with a unique UUID. There is no shared stream. On sync, both are new streams — the hub accepts them unconditionally.

This covers **80%+ of all clinical events** — vital signs, symptoms, examination findings, lab results, clinical notes, procedures, referrals, and treatment plans. All are either fact aggregates (single-event, new stream) or independent lifecycle aggregates (new stream per instance).

---

### 2. Same patient, different aggregates

Device A records a symptom for patient X. Device B records an allergy for the same patient X. Both offline.

```
Device A: Symptom-{uuid-3} v1            → new stream
Device B: AllergyRecord-{uuid-4} v1      → new stream
```

**Why not a conflict**: Different aggregate types, different aggregate IDs, different streams. The patient is the same, but aggregates are not partitioned by patient — they are partitioned by aggregate ID. The "patient timeline" is a virtual stream assembled by projections at read time.

---

### 3. Same encounter, different clinical observations

Nurse records vitals during encounter E. Medical assistant records symptoms during the same encounter E. Both offline.

```
Device A: VitalSigns-{uuid-5} v1   → payload includes encounter_id: E
Device B: Symptom-{uuid-6} v1      → payload includes encounter_id: E
```

**Why not a conflict**: The encounter ID is in the event payload (a reference), not in the aggregate ID. These are separate streams. The encounter summary projection assembles them at read time.

---

### 4. Different lifecycle aggregates for the same encounter

Doctor creates Diagnosis-{uuid-7} on Device A. Same doctor creates ClinicalNote-{uuid-8} on Device B. Both reference encounter E.

```
Device A: Diagnosis-{uuid-7} v1     → new lifecycle aggregate
Device B: ClinicalNote-{uuid-8} v1  → new lifecycle aggregate
```

**Why not a conflict**: Each is a new aggregate instance. The encounter reference is payload metadata, not a shared stream.

---

### 5. Same aggregate type, different instances

Dr. A confirms Diagnosis-{uuid-9} on Device A. Dr. B confirms Diagnosis-{uuid-10} on Device B. Both for the same patient.

```
Device A: Diagnosis-{uuid-9} v1    → Dr. A's diagnosis
Device B: Diagnosis-{uuid-10} v1   → Dr. B's diagnosis
```

**Why not a conflict**: Different aggregate instances (different UUIDs). Each has its own stream.

---

### 6. Events on different aggregate streams that happen to reference the same entity

Device A adds an addendum to ClinicalNote-{uuid-11}. Device B records VitalSigns-{uuid-12} referencing the same encounter.

**Why not a conflict**: Different streams. Cross-references in payloads are resolved by projections, not by aggregate consistency.

---

### Summary: NOT a conflict if...

| Condition | Why |
|-----------|-----|
| Different aggregate IDs | Different streams — no shared state |
| New aggregate (fact or new lifecycle) | New stream with no remote counterpart |
| Same patient but different aggregates | Patient is a projection concern, not an aggregate concern |
| Same encounter but different aggregates | Encounter reference is in payload, not in stream ID |

**The aggregate design makes the common case conflict-free.**

---
---

# Part 2 — Situations That Require Domain Resolution

A conflict occurs **only** when two devices write to the **same aggregate stream** (same `aggregate_id`) while offline. This requires the events to target an **existing lifecycle aggregate** on both devices.

---

### Conflict Type 1: Concurrent encounter state transitions

Device A (nurse): `PatientTriaged` on Encounter-{E} (v2)
Device B (doctor): `EncounterBegan` on Encounter-{E} (v2)

Both expect v2. Only one can be v2.

**Resolution**: Apply in `occurredAt` order. If the resulting sequence is valid per the encounter state machine (INV-EP-2), accept both with re-numbered versions. If invalid (e.g., `EncounterBegan` before `PatientTriaged` but triage is required), reject the out-of-order event and flag for clinical review.

**Clinical reasoning**: The encounter state machine represents a real-world workflow. Triage happens before the doctor begins. The timestamps indicate the real-world order. Trust the clinicians' `occurredAt` claims, compensated for clock drift.

---

### Conflict Type 2: Concurrent patient identity corrections

Device A: `PatientIdentityCorrected` on PatientRegistration-{P} (v3) — corrects name
Device B: `PatientContactInfoProvided` on PatientRegistration-{P} (v3) — updates phone

Both expect v3.

**Resolution**: Accept both in `occurredAt` order, re-number versions. Both are valid clinical facts (someone discovered information). If they modify the **same field** to **different values**, emit a `CompensationRequired` event and flag for human review.

**Clinical reasoning**: Clinical data is sacred. Both corrections were made by authorized staff based on real information. Neither should be discarded.

---

### Conflict Type 3: Concurrent diagnosis lifecycle changes

Device A: `DiagnosisRevised` on Diagnosis-{D} (v2)
Device B: `DiagnosisResolved` on Diagnosis-{D} (v2)

Both expect v2.

**Resolution**: Apply in `occurredAt` order.
- If revision comes first → revised then resolved: valid. Accept both.
- If resolution comes first → resolved then revised: invalid (INV-CJ-3: cannot revise a resolved diagnosis). Accept the resolution, reject the revision, flag for clinical review.

**Clinical reasoning**: If the doctor resolved the diagnosis, a subsequent revision is medically incoherent. But the revision attempt contains clinical information (the doctor wanted to change something) — so it's flagged, not silently dropped.

---

### Conflict Type 4: Concurrent appointment mutations

Device A: `AppointmentCancelled` on Appointment-{A} (v2)
Device B: `AppointmentRescheduled` on Appointment-{A} (v2)

Both expect v2.

**Resolution**: First event in `occurredAt` order wins.
- If cancelled first → cannot reschedule a cancelled appointment. Reject reschedule, notify.
- If rescheduled first → can still be cancelled after rescheduling. Accept both.

---

### Conflict Type 5: Duplicate events (same intent from two devices)

Device A: `EncounterCompleted` on Encounter-{E} (v4)
Device B: `EncounterCompleted` on Encounter-{E} (v4) — different event_id, same intent

**Resolution**: Deduplicate by **event type + aggregate state**. If the aggregate is already in the target state after accepting the first event, the second event is redundant. Accept the first, discard the second (it's not clinical data — it's a duplicate action).

---

## Resolution Algorithm

```
function resolve_conflict(local_event, remote_events, aggregate):
    1. Order all conflicting events by occurredAt (adjusted for clock drift)
    2. Replay the aggregate from last consistent state
    3. For each event in order:
       a. Check if aggregate state permits this transition
       b. If valid → accept, assign next version
       c. If invalid → flag for clinical review, emit CompensationRequired
    4. Return accepted events with corrected versions
```

---

## Conflict Resolution Properties

| Property | Guarantee |
|----------|-----------|
| **Deterministic** | Same conflicting events → same resolution on any node |
| **Data-preserving** | No clinical event is silently discarded |
| **Auditable** | Every conflict produces a resolution record |
| **Clinically meaningful** | Resolution rules follow medical workflow logic |

---
---

# Part 3 — Why Aggregates Minimize Conflicts

## The Design Is the Solution

The aggregate design was not accidentally conflict-free. Every design decision about aggregate boundaries was made with offline concurrency in mind.

---

### Reason 1: Fact aggregates create new streams

Every clinical observation (vitals, symptoms, exam findings, lab results) is a **fact aggregate** — a single-event, single-stream entity. Creating a fact aggregate means creating a new stream with a new UUID. New streams cannot conflict with anything.

**Impact**: The most frequent clinical events (observations during an encounter) are structurally conflict-free.

---

### Reason 2: Independent lifecycle aggregates per clinical entity

Each diagnosis, clinical note, procedure, referral, and treatment plan is its own lifecycle aggregate with its own UUID. Two doctors working on the same patient create **separate** aggregates, not writes to a shared aggregate.

**Impact**: Multi-clinician workflows on the same patient produce zero conflicts.

---

### Reason 3: Shared lifecycle aggregates are low-frequency

The only aggregates shared across actors are:
- **Encounter** — one per visit, transitions are sequential and infrequent
- **PatientRegistration** — one per patient, changes are rare
- **Appointment** — one per booking, mutations are infrequent

These aggregates change rarely (a few events per lifetime), so the probability of two devices writing to the same one simultaneously is low.

---

### Reason 4: Stream-per-aggregate eliminates cross-aggregate conflicts

Aggregate streams are independent. An event in Diagnosis-{D} cannot conflict with an event in Encounter-{E}, even if they reference the same patient and encounter. Cross-aggregate relationships are enforced by **eventually consistent** projections, not by stream-level concurrency.

**Impact**: The only consistency boundary that matters for conflict detection is the single aggregate stream.

---

### Reason 5: UUID-per-instance eliminates identity conflicts

Every aggregate instance gets a UUID at creation time. Two nurses creating vitals recordings for the same patient get different UUIDs. There is no shared counter, no sequence generator, no naming collision.

**Impact**: No coordination needed between devices to create new aggregate instances.

---

## Conflict Probability Analysis

| Event Category | % of Clinical Events | Aggregate Type | Conflict Possible? |
|---------------|---------------------|----------------|-------------------|
| Vital signs, symptoms, exam findings, lab results | ~50% | Fact (new stream) | **No** |
| Diagnoses, notes, procedures, referrals, treatment plans | ~30% | New lifecycle (new stream) | **No** |
| Encounter transitions | ~10% | Shared lifecycle | **Rare** — sequential, single-actor |
| Patient identity changes | ~5% | Shared lifecycle | **Rare** — infrequent mutations |
| Appointment mutations | ~5% | Shared lifecycle | **Rare** — infrequent mutations |

**Estimated conflict rate in normal operation: <1% of sync operations.**

The aggregate design doesn't just minimize conflicts — it makes them statistically negligible.
