"""Frozen flush decision: the basis captured at flush start.

The pressure differential and cumulative intake volume are read once when the
flush is decided and frozen for the whole run. Readings taken mid-flush never
change a decision that has already started.
"""

from dataclasses import dataclass
from typing import Any

from .policy import FlushPolicy


@dataclass(frozen=True)
class FlushDecision:
    """The frozen basis for one flush call.

    ``pressure_delta`` and ``intake_volume`` are the readings captured at
    decision time. They are immutable for the life of the resulting run.
    """

    asset: str
    pressure_delta: float
    intake_volume: float
    pressure_limit: float
    volume_limit: float
    policy: str
    pressure_due: bool
    volume_due: bool
    due: bool
    tick: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly projection; the same shape is stored and audited."""
        return {
            "asset": self.asset,
            "pressure_delta": self.pressure_delta,
            "intake_volume": self.intake_volume,
            "pressure_limit": self.pressure_limit,
            "volume_limit": self.volume_limit,
            "policy": self.policy,
            "pressure_due": self.pressure_due,
            "volume_due": self.volume_due,
            "due": self.due,
            "tick": self.tick,
        }


class FlushDecider:
    """Decides whether a flush is due from pressure differential and intake volume."""

    def __init__(
        self,
        pressure_limit: float,
        volume_limit: float,
        policy: FlushPolicy,
    ) -> None:
        if pressure_limit < 0 or volume_limit < 0:
            raise ValueError("flush limits must not be negative")
        self.pressure_limit = pressure_limit
        self.volume_limit = volume_limit
        self.policy = policy

    def decide(
        self,
        asset: str,
        pressure_delta: float,
        intake_volume: float,
        tick: int,
    ) -> FlushDecision:
        """Capture the readings and combine them under the asset's policy."""
        pressure_due = pressure_delta >= self.pressure_limit
        volume_due = intake_volume >= self.volume_limit
        due = self.policy.due(pressure_due, volume_due)
        return FlushDecision(
            asset=asset,
            pressure_delta=pressure_delta,
            intake_volume=intake_volume,
            pressure_limit=self.pressure_limit,
            volume_limit=self.volume_limit,
            policy=self.policy.name,
            pressure_due=pressure_due,
            volume_due=volume_due,
            due=due,
            tick=tick,
        )
