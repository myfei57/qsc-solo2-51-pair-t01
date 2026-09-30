"""Cumulative intake volume since the last flush.

Fouling tracks the total debris load, which is proportional to the volume of
water that has passed through the screen or rack, not to the instantaneous
flow. The accumulator is reset when a flush lands.
"""

from dataclasses import dataclass


@dataclass
class VolumeAccumulator:
    """Running total of intake volume, reset after a flush."""

    total: float = 0.0

    def accumulate(self, flow: float) -> float:
        """Add one sweep's flow and return the new cumulative volume."""
        if flow < 0:
            raise ValueError("intake flow must not be negative")
        self.total += flow
        return self.total

    def reset(self) -> float:
        """Clear the total after a flush and return what had accumulated."""
        accumulated = self.total
        self.total = 0.0
        return accumulated
