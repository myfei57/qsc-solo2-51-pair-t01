"""Flush trigger policy: how differential pressure and intake flow decide a flush.

The old trigger fired on a schedule alone (到点就冲) and ignored the process
signals. The trigger is now a policy that reads both the head loss across the
intake (压差) and the current intake flow (取水量), and each device class gets
its own trade-off between a false flush (误冲) and a missed flush (漏冲):

- ``SCREEN`` fouls gradually and a miss is recovered on the next sweep, so it
  flushes only when *both* signals trip — biased against false flushes.
- ``RACK`` blocks hard and a blockage is expensive, so it flushes when
  *either* signal trips — biased against missed flushes.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class DeviceClass(str, Enum):
    """The two kinds of intake hardware that are flushed."""

    SCREEN = "screen"
    RACK = "rack"

    def __str__(self) -> str:
        return self.value


class TriggerMode(str, Enum):
    """How the two signals combine into a flush verdict."""

    ALL = "all"  # every condition must hold: fewer false flushes
    ANY = "any"  # one condition suffices: fewer missed flushes

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class FlushPolicy:
    """Thresholds and combination mode for one device class."""

    device_class: DeviceClass
    mode: TriggerMode
    delta_limit: float
    flow_limit: float

    def evaluate(self, delta: float, flow: float) -> tuple[bool, tuple[str, ...]]:
        """Decide whether to flush and name the conditions that held."""
        held: list[str] = []
        if delta >= self.delta_limit:
            held.append("delta")
        if flow >= self.flow_limit:
            held.append("flow")
        if self.mode is TriggerMode.ALL:
            return len(held) == 2, tuple(held)
        return len(held) >= 1, tuple(held)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_class": self.device_class.value,
            "mode": self.mode.value,
            "delta_limit": self.delta_limit,
            "flow_limit": self.flow_limit,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "FlushPolicy":
        """Rebuild a policy from a persisted row."""
        return cls(
            device_class=DeviceClass(row["device_class"]),
            mode=TriggerMode(row["mode"]),
            delta_limit=float(row["delta_limit"]),
            flow_limit=float(row["flow_limit"]),
        )


# The screen limit matches the cleaning limit used across the console; the flow
# limit asks that the fouling signal is confirmed under a meaningful draw.
SCREEN_POLICY = FlushPolicy(
    device_class=DeviceClass.SCREEN,
    mode=TriggerMode.ALL,
    delta_limit=30.0,
    flow_limit=50.0,
)

# The rack trips on a lower head loss and treats a heavy draw as a trigger on
# its own, because a heavy draw drags debris into the rack before the loss
# climbs.
RACK_POLICY = FlushPolicy(
    device_class=DeviceClass.RACK,
    mode=TriggerMode.ANY,
    delta_limit=20.0,
    flow_limit=90.0,
)


def default_policies() -> dict[str, FlushPolicy]:
    """The per-device policies of the line, keyed by device id."""
    return {
        DeviceClass.SCREEN.value: SCREEN_POLICY,
        DeviceClass.RACK.value: RACK_POLICY,
    }
