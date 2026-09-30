"""Projections of the flush record and the three-way consistency check.

The stored record is the pivot. The audit events are written when the
decision is frozen and when the run finishes; the console display and the
external data feed both project the same stored record. The checks here
recompute the digests on each surface and confirm they still agree.
"""

import json
from dataclasses import dataclass
from typing import Any

from ..audit.recorder import Recorder
from ..store import keys
from ..store.checksum import digest
from ..store.store import Store
from .decision import FlushDecision
from .record import FlushRecord

DECISION_EVENT = "flush-decision"
OUTCOME_EVENT = "flush-outcome"
DUPLICATE_EVENT = "flush-duplicate"


def decision_digest(decision: FlushDecision) -> str:
    """Digest of a frozen decision, as referenced by the decision event."""
    return digest({"decision": decision.to_dict()})


def record_digest(record: FlushRecord) -> str:
    """Digest of a whole record, as referenced by the outcome event."""
    return digest({"flush": record.to_dict()})


def projection(record: FlushRecord) -> dict[str, Any]:
    """The canonical per-run view shared by the console and the external feed."""
    return {
        "run_id": record.run_id,
        "device_id": record.device_id,
        "device_class": record.decision.policy.device_class.value,
        "verdict": record.decision.verdict,
        "outcome": record.outcome,
        "failed_step": record.execution.failed_step,
        "reverted": record.execution.reverted,
        "started_tick": record.started_tick,
        "finished_tick": record.finished_tick,
        "decision_digest": decision_digest(record.decision),
        "record_digest": record_digest(record),
    }


def _event_payload(event) -> dict[str, Any]:
    """Parse the JSON detail of a flush audit event."""
    try:
        payload = json.loads(event.detail)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _find_event(recorder: Recorder, kind: str, run_id: str):
    """The newest audit event of ``kind`` that names ``run_id``, if any."""
    for event in recorder.events():
        if event.kind != kind:
            continue
        payload = _event_payload(event)
        if payload.get("run_id") == run_id:
            return payload
    return None


@dataclass(frozen=True)
class RunCheck:
    """Whether one record still agrees with the audit and the projection."""

    run_id: str
    consistent: bool
    decision_digest: str
    record_digest: str
    audit_decision_digest: str | None
    audit_record_digest: str | None
    projection_digest: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "consistent": self.consistent,
            "decision_digest": self.decision_digest,
            "record_digest": self.record_digest,
            "audit_decision_digest": self.audit_decision_digest,
            "audit_record_digest": self.audit_record_digest,
            "projection_digest": self.projection_digest,
            "detail": self.detail,
        }


def verify_run(store: Store, recorder: Recorder, sequence: int) -> RunCheck:
    """Check one stored record against the audit events and the projection."""
    row = store.get(keys.flush_run_key(sequence))
    if row is None:
        return RunCheck(
            run_id="",
            consistent=False,
            decision_digest="",
            record_digest="",
            audit_decision_digest=None,
            audit_record_digest=None,
            projection_digest="",
            detail=f"no flush record at sequence {sequence}",
        )
    record = FlushRecord.from_dict(row)
    view = projection(record)
    decision_event = _find_event(recorder, DECISION_EVENT, record.run_id)
    outcome_event = _find_event(recorder, OUTCOME_EVENT, record.run_id)

    audit_decision_digest = (
        str(decision_event.get("decision_digest")) if decision_event else None
    )
    audit_record_digest = (
        str(outcome_event.get("record_digest")) if outcome_event else None
    )

    problems: list[str] = []
    if decision_event is None:
        problems.append("no flush-decision event")
    elif audit_decision_digest != view["decision_digest"]:
        problems.append("decision digest differs from the audit")
    if outcome_event is None:
        problems.append("no flush-outcome event")
    elif audit_record_digest != view["record_digest"]:
        problems.append("record digest differs from the audit")
    if view["record_digest"] != record_digest(record):
        problems.append("projection drifted from the record")

    return RunCheck(
        run_id=record.run_id,
        consistent=not problems,
        decision_digest=view["decision_digest"],
        record_digest=view["record_digest"],
        audit_decision_digest=audit_decision_digest,
        audit_record_digest=audit_record_digest,
        projection_digest=view["record_digest"],
        detail="; ".join(problems) if problems else "ok",
    )


def verify_all(store: Store, recorder: Recorder, highest: int) -> dict[str, Any]:
    """Check every stored flush record and report the overall consistency."""
    checks = [verify_run(store, recorder, sequence) for sequence in range(1, highest + 1)]
    return {
        "consistent": all(check.consistent for check in checks),
        "checked": len(checks),
        "runs": [check.to_dict() for check in checks],
    }
