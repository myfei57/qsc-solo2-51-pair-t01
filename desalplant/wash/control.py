"""Flush orchestration: decide, guard, record, execute, audit.

One request flows through a fixed pipeline:

1. the decision is computed and frozen from the current readings;
2. the guard makes concurrent requests for the same device idempotent;
3. the decision is written to the audit before anything moves;
4. the wash program runs, protected by the pump interlock;
5. the record is persisted and the outcome is written to the audit.

The stored record is the single source of truth that the console display and
the external data feed project, so all three surfaces stay checkable.
"""

import json
import threading
from dataclasses import dataclass
from typing import Any

from ..audit.recorder import Recorder
from ..clock import LogicalClock
from ..ns.ids import new_id
from ..store import keys
from ..store.store import Store
from . import report
from .decision import FlushDecision
from .guard import FlushGuard
from .policy import FlushPolicy
from .program import steps_for
from .record import FlushRecord
from .run import WashExecutor

FLUSHED = "flushed"
DUPLICATE = "duplicate"
NOT_NEEDED = "not-needed"


@dataclass(frozen=True)
class FlushResult:
    """The answer to one flush request."""

    decision: FlushDecision
    status: str
    record: FlushRecord | None
    inflight_run_id: str | None

    @property
    def run_id(self) -> str:
        """The run this request attaches to: its own, or the in-flight one."""
        return self.inflight_run_id or self.decision.run_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "device_id": self.decision.device_id,
            "status": self.status,
            "verdict": self.decision.verdict,
            "decision": self.decision.to_dict(),
            "record": self.record.to_dict() if self.record is not None else None,
        }


class WashController:
    """Runs the decide-guard-record-execute-audit pipeline per device."""

    def __init__(
        self,
        store: Store,
        recorder: Recorder,
        clock: LogicalClock,
        pump,
        devices: dict[str, Any],
        policies: dict[str, FlushPolicy],
    ) -> None:
        self._store = store
        self._recorder = recorder
        self._clock = clock
        self._devices = devices
        self._policies = policies
        self._guard = FlushGuard()
        self._executor = WashExecutor(pump, devices)
        self._persist_lock = threading.Lock()
        self._sequence = int(store.get(keys.FLUSH_SEQUENCE, 0) or 0)

    @property
    def sequence(self) -> int:
        """Sequence number of the newest flush record."""
        with self._persist_lock:
            return self._sequence

    def policies(self) -> dict[str, FlushPolicy]:
        """The per-device policies, keyed by device id."""
        return dict(self._policies)

    # ------------------------------------------------------------- decisions

    def evaluate(self, device_id: str, delta: float, flow: float) -> FlushDecision:
        """Compute the frozen decision for the current readings."""
        policy = self._policies[device_id]
        verdict, reasons = policy.evaluate(delta, flow)
        return FlushDecision(
            run_id=new_id(),
            device_id=device_id,
            policy=policy,
            delta=float(delta),
            flow=float(flow),
            verdict=verdict,
            reasons=reasons,
            tick=self._clock.tick,
        )

    def request_flush(self, device_id: str, delta: float, flow: float) -> FlushResult:
        """Evaluate the readings and flush when the policy says so."""
        decision = self.evaluate(device_id, delta, flow)
        if not decision.verdict:
            return FlushResult(
                decision=decision, status=NOT_NEEDED, record=None, inflight_run_id=None
            )
        inflight = self._guard.try_acquire(device_id, decision.run_id)
        if inflight is not None:
            self._log(report.DUPLICATE_EVENT, {"run_id": inflight, "device_id": device_id})
            return FlushResult(
                decision=decision,
                status=DUPLICATE,
                record=None,
                inflight_run_id=inflight,
            )
        try:
            return self._run(decision)
        finally:
            self._guard.release(device_id, decision.run_id)

    def _run(self, decision: FlushDecision) -> FlushResult:
        """Audit, execute and record one flush."""
        self._log(
            report.DECISION_EVENT,
            {
                "run_id": decision.run_id,
                "device_id": decision.device_id,
                "verdict": decision.verdict,
                "delta": decision.delta,
                "flow": decision.flow,
                "reasons": list(decision.reasons),
                "decision_digest": report.decision_digest(decision),
            },
        )
        execution = self._executor.execute(
            decision.device_id,
            decision.policy.device_class,
            steps_for(decision.policy.device_class),
        )
        record = FlushRecord(
            decision=decision,
            execution=execution,
            started_tick=decision.tick,
            finished_tick=self._clock.tick,
        )
        self._persist(record)
        self._log(
            report.OUTCOME_EVENT,
            {
                "run_id": record.run_id,
                "device_id": record.device_id,
                "outcome": record.outcome,
                "failed_step": execution.failed_step,
                "reverted": execution.reverted,
                "record_digest": report.record_digest(record),
            },
        )
        return FlushResult(
            decision=decision, status=FLUSHED, record=record, inflight_run_id=None
        )

    def _persist(self, record: FlushRecord) -> None:
        """Append the record and point the device's latest slot at it.

        Two devices can flush at the same time on the threaded console, so the
        sequence step and the writes happen under a lock: two records never
        share a sequence number. The sequence key is written last, as the
        commit point a reader can trust.
        """
        with self._persist_lock:
            self._sequence += 1
            sequence = self._sequence
            self._store.put(keys.flush_run_key(sequence), record.to_dict())
            self._store.put(keys.flush_latest_key(record.device_id), sequence)
            self._store.put(keys.FLUSH_SEQUENCE, sequence)

    def _log(self, kind: str, payload: dict[str, Any]) -> None:
        """Write one audit event with a machine-checkable JSON detail."""
        self._recorder.log(kind, json.dumps(payload, sort_keys=True))

    # --------------------------------------------------------------- reading

    def record_at(self, sequence: int) -> FlushRecord | None:
        """The record stored at ``sequence``, or ``None`` when absent."""
        row = self._store.get(keys.flush_run_key(sequence))
        if row is None:
            return None
        return FlushRecord.from_dict(row)

    def records(self, limit: int = 0) -> tuple[FlushRecord, ...]:
        """Newest-first records, capped at ``limit`` when it is positive."""
        highest = self.sequence
        if highest <= 0:
            return ()
        bound = highest if limit <= 0 else min(limit, highest)
        collected: list[FlushRecord] = []
        for sequence in range(highest, highest - bound, -1):
            record = self.record_at(sequence)
            if record is not None:
                collected.append(record)
        return tuple(collected)

    def latest_record(self, device_id: str) -> FlushRecord | None:
        """A device's newest record, or ``None`` when it never flushed."""
        sequence = int(self._store.get(keys.flush_latest_key(device_id), 0) or 0)
        if sequence <= 0:
            return None
        return self.record_at(sequence)

    def inflight(self, device_id: str) -> str | None:
        """The run currently in flight for a device, if any."""
        return self._guard.current(device_id)

    # ------------------------------------------------------------- reporting

    def verify(self, sequence: int) -> report.RunCheck:
        """Check one record against the audit and the projection."""
        return report.verify_run(self._store, self._recorder, sequence)

    def verify_all(self) -> dict[str, Any]:
        """Check every record and report the overall consistency."""
        return report.verify_all(self._store, self._recorder, self._sequence)
