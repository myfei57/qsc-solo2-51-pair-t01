"""The frozen basis of a flush decision.

A decision is computed once, when a flush is requested, from the readings
taken at that moment. It is frozen: the run that follows executes against
this snapshot and never re-reads the sensors. The only live input during a
run is the pump-running interlock, which protects the run but never changes
the verdict. This is the "freeze at flush start" rule, fixed for the line.
"""

from dataclasses import dataclass
from typing import Any, Mapping

from .policy import FlushPolicy


@dataclass(frozen=True)
class FlushDecision:
    """An immutable record of why a flush was or was not demanded."""

    run_id: str
    device_id: str
    policy: FlushPolicy
    delta: float
    flow: float
    verdict: bool
    reasons: tuple[str, ...]
    tick: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "device_id": self.device_id,
            "policy": self.policy.to_dict(),
            "delta": self.delta,
            "flow": self.flow,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "tick": self.tick,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "FlushDecision":
        """Rebuild a decision from a persisted row."""
        return cls(
            run_id=str(row["run_id"]),
            device_id=str(row["device_id"]),
            policy=FlushPolicy.from_dict(row["policy"]),
            delta=float(row["delta"]),
            flow=float(row["flow"]),
            verdict=bool(row["verdict"]),
            reasons=tuple(row.get("reasons", ())),
            tick=int(row.get("tick", 0)),
        )
