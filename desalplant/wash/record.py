"""The persisted flush record: the single source of truth for a run.

The console display and the external data feed both project this record, and
the audit events reference it, so the three surfaces can be checked against
each other.
"""

from dataclasses import dataclass
from typing import Any, Mapping

from .decision import FlushDecision
from .run import Execution


@dataclass(frozen=True)
class FlushRecord:
    """A flush run: its frozen decision, its execution and its ticks."""

    decision: FlushDecision
    execution: Execution
    started_tick: int
    finished_tick: int

    @property
    def run_id(self) -> str:
        return self.decision.run_id

    @property
    def device_id(self) -> str:
        return self.decision.device_id

    @property
    def outcome(self) -> str:
        return self.execution.outcome

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "device_id": self.device_id,
            "decision": self.decision.to_dict(),
            "execution": self.execution.to_dict(),
            "started_tick": self.started_tick,
            "finished_tick": self.finished_tick,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "FlushRecord":
        """Rebuild a record from a persisted row."""
        return cls(
            decision=FlushDecision.from_dict(row["decision"]),
            execution=Execution.from_dict(row["execution"]),
            started_tick=int(row.get("started_tick", 0)),
            finished_tick=int(row.get("finished_tick", 0)),
        )
