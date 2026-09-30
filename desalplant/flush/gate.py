"""Idempotent flush gate and the staged flush run it serialises.

The gate is keyed by asset (``screen`` / ``rack``). Concurrent requests for the
same asset return the in-flight run instead of starting a second one, so a
screen or rack can never be flushed twice.
"""

import threading
from dataclasses import dataclass, field
from typing import Any

from .decision import FlushDecision
from .stage import Stage


@dataclass(frozen=True)
class StageResult:
    """Outcome of advancing one flush stage."""

    completed: bool
    stage: str = ""
    failed_step: str = ""
    safe_position: bool = False
    already_finished: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed": self.completed,
            "stage": self.stage,
            "failed_step": self.failed_step,
            "safe_position": self.safe_position,
            "already_finished": self.already_finished,
        }


@dataclass
class FlushRun:
    """One flush execution against a frozen decision.

    The decision is captured at construction and never changes. Each stage is
    advanced one at a time; if the pump stops mid-flush the run retreats to the
    safe position and records which step failed.
    """

    asset: str
    decision: FlushDecision
    stages: tuple[Stage, ...]
    current: int = 0
    finished: bool = False
    failed_step: str | None = None
    safe_position: bool = False

    def step(self, pump_running: bool) -> StageResult:
        """Advance the next stage.

        A stopped pump before the run finishes is a partial failure: the run
        stops where it is, retreats to the safe position and names the step
        that did not complete.
        """
        if self.finished:
            return StageResult(completed=True, already_finished=True)
        stage = self.stages[self.current]
        if not pump_running:
            self.failed_step = stage.name
            self._return_to_safe_position()
            return StageResult(
                completed=False,
                failed_step=stage.name,
                safe_position=True,
            )
        self.current += 1
        if self.current >= len(self.stages):
            self.finished = True
        return StageResult(completed=True, stage=stage.name)

    def _return_to_safe_position(self) -> None:
        """Retreat to the safe position after a partial failure."""
        self.safe_position = True
        self.finished = True

    def to_dict(self) -> dict[str, Any]:
        """Projection of the run state, including the frozen decision."""
        return {
            "asset": self.asset,
            "decision": self.decision.to_dict(),
            "stages": [stage.to_dict() for stage in self.stages],
            "current": self.current,
            "finished": self.finished,
            "failed_step": self.failed_step,
            "safe_position": self.safe_position,
        }


class FlushGate:
    """Serialises flush requests per asset so one asset never flushes twice."""

    def __init__(self) -> None:
        self._in_flight: dict[str, FlushRun] = {}
        self._lock = threading.Lock()

    def request(self, run: FlushRun) -> FlushRun:
        """Return the in-flight run for the asset, or register this one.

        A second request for an asset that already has an unfinished run gets
        the existing run back. It does not start a new flush.
        """
        with self._lock:
            existing = self._in_flight.get(run.asset)
            if existing is not None and not existing.finished:
                return existing
            self._in_flight[run.asset] = run
            return run

    def finish(self, run: FlushRun) -> None:
        """Drop the in-flight run once it has finished or failed safe."""
        with self._lock:
            current = self._in_flight.get(run.asset)
            if current is run:
                self._in_flight.pop(run.asset, None)

    def in_flight(self, asset: str) -> bool:
        """Whether the asset currently has an unfinished flush run."""
        with self._lock:
            run = self._in_flight.get(asset)
            return run is not None and not run.finished
