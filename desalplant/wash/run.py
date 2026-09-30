"""Step-by-step wash execution with pump-stop protection.

Every step runs only while the intake pump is running. If the pump stops
mid-run the wash is a partial failure: the current step is marked failed, the
remaining steps are skipped, the device is returned to its safe position, and
the failed step is recorded so the audit shows exactly what was not done.
"""

from dataclasses import dataclass
from typing import Any, Mapping

from .policy import DeviceClass

SAFE_POSITION = "idle"

SUCCESS = "success"
PARTIAL = "partial"

DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass(frozen=True)
class StepOutcome:
    """How one step of a wash program ended."""

    name: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status}

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "StepOutcome":
        """Rebuild a step outcome from a persisted row."""
        return cls(name=str(row["name"]), status=str(row["status"]))


@dataclass(frozen=True)
class Execution:
    """The result of running a wash program."""

    outcome: str
    steps: tuple[StepOutcome, ...]
    failed_step: str | None
    reverted: bool
    position: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "steps": [step.to_dict() for step in self.steps],
            "failed_step": self.failed_step,
            "reverted": self.reverted,
            "position": self.position,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "Execution":
        """Rebuild an execution from a persisted row."""
        return cls(
            outcome=str(row["outcome"]),
            steps=tuple(StepOutcome.from_dict(step) for step in row.get("steps", ())),
            failed_step=row.get("failed_step"),
            reverted=bool(row.get("reverted", False)),
            position=str(row.get("position", SAFE_POSITION)),
        )


class WashExecutor:
    """Runs a wash program against a device, watching the pump."""

    def __init__(self, pump, devices) -> None:
        self._pump = pump
        self._devices = devices

    def execute(
        self, device_id: str, device_class: DeviceClass, steps: tuple[str, ...]
    ) -> Execution:
        """Run ``steps`` in order, stopping at the first pump outage."""
        device = self._devices[device_id]
        outcomes: list[StepOutcome] = []
        failed: str | None = None
        for step in steps:
            if failed is not None:
                outcomes.append(StepOutcome(name=step, status=SKIPPED))
                continue
            running = bool(self._pump.running)
            if not running:
                outcomes.append(StepOutcome(name=step, status=FAILED))
                failed = step
                continue
            self._apply(device_class, device, step, running)
            outcomes.append(StepOutcome(name=step, status=DONE))
        if failed is None:
            return Execution(
                outcome=SUCCESS,
                steps=tuple(outcomes),
                failed_step=None,
                reverted=False,
                position=SAFE_POSITION,
            )
        self._safe_position(device_class, device)
        return Execution(
            outcome=PARTIAL,
            steps=tuple(outcomes),
            failed_step=failed,
            reverted=True,
            position=SAFE_POSITION,
        )

    def _apply(self, device_class: DeviceClass, device, step: str, running: bool) -> None:
        """The device-side effect of one step."""
        if device_class is DeviceClass.SCREEN:
            if step == "isolate":
                device.isolate()
            elif step == "backwash":
                device.clear()
            elif step == "restore":
                device.restore()
        elif device_class is DeviceClass.RACK:
            if step == "rinse":
                device.rinse(running)

    def _safe_position(self, device_class: DeviceClass, device) -> None:
        """Return the device to its safe position after a partial failure."""
        if device_class is DeviceClass.SCREEN:
            device.restore()
