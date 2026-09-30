"""Wiring of the subsystems behind the console actions."""

from typing import Any

from ..audit.event import Event
from ..audit.recorder import Recorder
from ..brine.stream import BrineStream
from ..brine.valve import Valve
from ..chem.doser import Doser
from ..chem.tank import ChemicalTank
from ..clock import LogicalClock
from ..filter.media import MediaFilter
from ..filter.rack import Rack
from ..flush.rinse import RinseCycle
from ..flush.start import Flusher
from ..intake.feed import Feed
from ..intake.gate import Gate
from ..intake.interlock import GateInterlock
from ..intake.pump import IntakePump
from ..intake.screen import Screen
from ..ns.state import PlantState
from ..pump import differential, new, ramp, ramp_pressure, stop
from ..ro.array import MembraneArray
from ..ro.flow import FlowCheck
from ..ro.membrane import Membrane
from ..ro.output import Output
from ..ro.permeate import Permeate
from ..ro.recovery import Recovery
from ..store import keys
from ..store.snapshot import Snapshot
from ..store.store import Store
from ..tank.handoff import handoff
from ..tank.level import Level
from ..tank.product import ProductTank
from ..tank.tanks import TankSet
from ..wash.control import FlushResult, WashController
from ..wash.policy import default_policies
from ..wash.report import projection as flush_projection
from . import alarms, audit_report, integration, probes, reports, summary, telemetry, version
from .version import BuildInfo

RAMP_TARGET = 60.0
PRODUCE_CONDUCTIVITY = 120.0
PRODUCE_POWER = 100.0
PRODUCE_LEVEL = 68.0
MAINTENANCE_EFFICIENCY = 0.8
MAINTENANCE_DRAW = 0.1
LAST_STAGE_READINGS = {"s3": 5.0}


class Plant:
    """Every subsystem of the line, wired together behind the console."""

    def __init__(self, store: Store, clock: LogicalClock, recorder: Recorder) -> None:
        self.store = store
        self.clock = clock
        self.recorder = recorder
        self.state = PlantState.IDLE

        self.feed = Feed()
        self.intake_pump = IntakePump()
        self.gate = Gate()
        self.interlock = GateInterlock(enabled=True)
        self.screen = Screen()
        self.media = MediaFilter()
        self.rack = Rack()
        self.doser = Doser()
        self.chem_tank = ChemicalTank(level=100.0)
        self.membrane = Membrane(flow=80.0, min_flow=50.0)
        self.permeate = Permeate(max_cond=300.0)
        self.array = MembraneArray(2)
        self.output = Output()
        self.recovery = Recovery()
        self.valve = Valve()
        self.hp_pump = new()
        self.flusher = Flusher()
        self.rinse = RinseCycle()
        self.tank_set = TankSet(current="tank-a", standby="tank-b")
        self.product = ProductTank()
        self.level = Level()
        self.brine_stream = BrineStream(concentration=0.6)

        self.wash = WashController(
            store=self.store,
            recorder=self.recorder,
            clock=self.clock,
            pump=self.intake_pump,
            devices={"screen": self.screen, "rack": self.rack},
            policies=default_policies(),
        )

    # ------------------------------------------------------------- lifecycle

    def start(self) -> Event:
        """Bring the line up: intake, media, dosing, flow and pressure."""
        self.clock.advance()
        self.gate.open_gate()
        self.media.backwash()
        self.media.require_ready()
        self.intake_pump.start()
        self.feed.start()
        self.doser.start(self.feed)
        self.hp_pump.start()
        ramp(self.hp_pump, self.membrane, RAMP_TARGET)
        ramp_pressure(self.hp_pump, self.valve, RAMP_TARGET)
        self.state = PlantState.RUNNING
        self.store.put(keys.PLANT_STATE, self.state.value)
        self.store.put(keys.PLANT_STARTED_AT, self.clock.tick)
        return self.recorder.log("start", "line started")

    def produce(self) -> Event:
        """Produce permeate and hand it to the tank when the quality allows."""
        self.clock.advance()
        check = FlowCheck()
        check.measure(self.membrane)
        self.permeate.update_conductivity(PRODUCE_CONDUCTIVITY)
        self.product.fill(self.permeate)
        self.output.check(self.hp_pump.efficiency, PRODUCE_POWER)
        self.level.update(PRODUCE_LEVEL)
        return self.recorder.log("produce", "permeate produced")

    def shutdown(self) -> Event:
        """Stop the line after stopping dosing, flushing and rinsing."""
        self.clock.advance()
        self.doser.stop(self.feed)
        stop(self.hp_pump, self.flusher)
        self.intake_pump.shutdown(self.rack)
        self.state = PlantState.IDLE
        self.store.put(keys.PLANT_STATE, self.state.value)
        self.store.put(keys.PLANT_STOPPED_AT, self.clock.tick)
        return self.recorder.log("shutdown", "line stopped")

    def switch_tank(self) -> Event:
        """Move production to the standby tank."""
        self.clock.advance()
        active = handoff(self.tank_set, self.hp_pump)
        return self.recorder.log("switch", f"product tank switched to {active}")

    def maintain(self) -> Event:
        """Run the maintenance routine and record it."""
        self.clock.advance()
        self.array.regroup(3)
        self.pump_differential()
        self.hp_pump.recalibrate(MAINTENANCE_EFFICIENCY)
        self.output.check(self.hp_pump.efficiency, PRODUCE_POWER)
        self.recovery.raise_alarm()
        self.valve.set_emergency()
        from ..brine.recovery import recover_alarm

        recover_alarm(self.valve, self.recovery)
        self.chem_tank.draw(MAINTENANCE_DRAW)
        self.rinse.run()
        self.store.put(keys.PLANT_MAINTAINED_AT, self.clock.tick)
        return self.recorder.log("maintain", "maintenance completed")

    def pump_differential(self) -> float:
        """Pressure differential over the array's final stage."""
        return differential(self.hp_pump, self.array, dict(LAST_STAGE_READINGS))

    # ------------------------------------------------------------------ wash

    def flush_check(self, device_id: str) -> FlushResult:
        """Evaluate the current readings and flush the device when due."""
        self.clock.advance()
        return self.wash.request_flush(device_id, self.screen.delta, self.membrane.flow)

    def flush_check_all(self) -> dict[str, FlushResult]:
        """Evaluate every washable device against one frozen sweep.

        The readings are captured once, because a screen backwash clears the
        shared head-loss signal: evaluating each device against a live reading
        would make the outcome depend on the order the devices are checked.
        """
        self.clock.advance()
        delta = self.screen.delta
        flow = self.membrane.flow
        return {
            device_id: self.wash.request_flush(device_id, delta, flow)
            for device_id in ("screen", "rack")
        }

    def flush_evaluation(self, device_id: str) -> dict[str, Any]:
        """The live, side-effect-free verdict for the current readings."""
        decision = self.wash.evaluate(device_id, self.screen.delta, self.membrane.flow)
        return {
            "delta": decision.delta,
            "flow": decision.flow,
            "verdict": decision.verdict,
            "reasons": list(decision.reasons),
        }

    def flush_display(self) -> dict[str, Any]:
        """Console view of the wash: policy, live verdict, records, consistency."""
        devices: dict[str, Any] = {}
        for device_id, policy in self.wash.policies().items():
            latest = self.wash.latest_record(device_id)
            devices[device_id] = {
                "policy": policy.to_dict(),
                "evaluation": self.flush_evaluation(device_id),
                "inflight": self.wash.inflight(device_id),
                "latest": flush_projection(latest) if latest is not None else None,
            }
        return {
            "devices": devices,
            "runs": [flush_projection(record) for record in self.wash.records(10)],
            "consistency": self.wash.verify_all(),
        }

    def flush_records(self, limit: int = 0) -> dict[str, Any]:
        """External feed of the wash records, projected from the same store."""
        records = self.wash.records(limit)
        return {
            "count": self.wash.sequence,
            "records": [flush_projection(record) for record in records],
        }

    def flush_verify(self) -> dict[str, Any]:
        """Three-way consistency check across store, audit and projection."""
        return self.wash.verify_all()

    # --------------------------------------------------------------- reading

    def status(self) -> dict[str, Any]:
        """Compact view of the line's current state."""
        checkpoint = self.snapshot()
        started = self.store.has(keys.PLANT_STARTED_AT)
        started_at = int(self.store.require(keys.PLANT_STARTED_AT)) if started else 0
        running_ticks = self.clock.elapsed(started_at) if started_at <= self.clock.tick else 0
        return {
            "state": str(self.state),
            "tick": self.clock.tick,
            "started": started,
            "running_ticks": running_ticks,
            "feed_flowing": self.feed.flowing,
            "feed_scaled": self.feed.scaled,
            "membrane": str(self.membrane),
            "valve_open": self.valve.open,
            "valve_stressed": self.valve.stressed,
            "valve_emergency": self.valve.emergency,
            "pump_pressure": self.hp_pump.pressure,
            "supply": self.hp_pump.supply,
            "stalled": self.hp_pump.stalled,
            "dumping": self.product.dumping,
            "filling": self.product.filling,
            "rack_blocked": self.rack.blocked,
            "flush_started": self.flusher.started,
            "flush_effective": self.flusher.effective,
            "store_entries": checkpoint.count,
        }

    def report(self) -> dict[str, Any]:
        """Numeric report over the pump, permeate and schedules."""
        return reports.build_report(self)

    def audit_report(self) -> dict[str, Any]:
        """Numeric report over the train, histograms and schedules."""
        return audit_report.build_audit_report(self)

    def integration(self) -> dict[str, Any]:
        """Report that exercises the peripheral components in one pass."""
        return integration.build_integration(self)

    def summary(self) -> dict[str, Any]:
        """Summary over the curves, stream arithmetic and membranes."""
        return summary.build_summary(self)

    def telemetry(self):
        """One telemetry sweep."""
        return telemetry.telemetry(self)

    def diagnostics(self) -> dict[str, Any]:
        """Structural diagnostics."""
        return telemetry.diagnostics(self)

    def alarms(self) -> list[dict[str, Any]]:
        """Current alarms."""
        return alarms.alarms(self)

    def selftest(self) -> dict[str, Any]:
        """Drive the whole line once and report every step."""
        return probes.build_selftest(self)

    def events(self, limit: int = 0) -> tuple[Event, ...]:
        """Newest-first audit events."""
        return self.recorder.events(limit)

    def event_count(self) -> int:
        """Number of audit events recorded."""
        return self.recorder.count()

    def build(self) -> BuildInfo:
        """Describe the running build."""
        return version.build()

    def snapshot(self) -> Snapshot:
        """Frozen view of the persisted document."""
        return self.store.snapshot()
