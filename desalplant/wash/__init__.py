"""Intake screen and rack wash: policy-driven flush with an audited record.

This is the intake wash for the screen (格栅) and the rack (机架), distinct
from the membrane flush in :mod:`desalplant.flush`. The trigger reads both
the head loss and the intake flow, the decision is frozen at flush start, and
every run lands in a persisted record that the console, the external feed and
the audit all agree on.
"""

from .control import FlushResult, WashController
from .decision import FlushDecision
from .guard import FlushGuard
from .policy import DeviceClass, FlushPolicy, TriggerMode, default_policies
from .program import steps_for
from .record import FlushRecord
from .run import Execution, StepOutcome, WashExecutor

__all__ = [
    "DeviceClass",
    "Execution",
    "FlushDecision",
    "FlushGuard",
    "FlushPolicy",
    "FlushRecord",
    "FlushResult",
    "StepOutcome",
    "TriggerMode",
    "WashController",
    "WashExecutor",
    "default_policies",
    "steps_for",
]
