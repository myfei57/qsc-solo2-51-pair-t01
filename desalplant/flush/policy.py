"""Flush decision policies: how pressure differential and intake volume combine.

The two assets have opposite false-flush / missed-flush tradeoffs, so each gets
its own combine rule instead of one shared threshold.
"""

from dataclasses import dataclass
from typing import Any, ClassVar


@dataclass(frozen=True)
class FlushPolicy:
    """Combine rule for the two fouling signals of one asset class.

    - ``"or"``: either signal triggers a flush. Favour a false flush over a
      missed one, because a clogged screen starves the intake pump and can tear
      the membranes.
    - ``"and"``: both signals must agree. Favour a missed flush over a false
      one, because a rack rinse wastes treated water and risks a mid-rinse pump
      stop.
    """

    name: str
    combine: str

    SCREEN: ClassVar["FlushPolicy"]
    RACK: ClassVar["FlushPolicy"]

    def due(self, pressure_due: bool, volume_due: bool) -> bool:
        """Whether the two signals, combined for this policy, demand a flush."""
        if self.combine == "or":
            return pressure_due or volume_due
        if self.combine == "and":
            return pressure_due and volume_due
        raise ValueError(f"unknown flush combine rule: {self.combine!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "combine": self.combine}


FlushPolicy.SCREEN = FlushPolicy(name="screen", combine="or")
FlushPolicy.RACK = FlushPolicy(name="rack", combine="and")

#: 格栅：压差高或取水量大都冲（宁误冲勿漏冲）。
SCREEN_POLICY = FlushPolicy.SCREEN

#: 机架：压差高且取水量大都满足才冲（宁漏冲勿误冲）。
RACK_POLICY = FlushPolicy.RACK
