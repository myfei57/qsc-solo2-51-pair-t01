"""Intake wash: policy trigger, frozen decision, idempotency and the record."""

import threading

import pytest

from desalplant.audit.recorder import Recorder
from desalplant.clock import LogicalClock
from desalplant.filter.rack import Rack
from desalplant.intake.screen import Screen
from desalplant.store import keys
from desalplant.store.store import Store
from desalplant.wash.control import WashController
from desalplant.wash.decision import FlushDecision
from desalplant.wash.guard import FlushGuard
from desalplant.wash.policy import (
    RACK_POLICY,
    SCREEN_POLICY,
    DeviceClass,
    FlushPolicy,
    TriggerMode,
    default_policies,
)
from desalplant.wash.program import steps_for
from desalplant.wash.record import FlushRecord
from desalplant.wash.report import decision_digest, record_digest

from support import ok


class ScriptedPump:
    """Pump whose running flag flips off after ``stop_after`` checks."""

    def __init__(self, stop_after):
        self._checks = 0
        self._stop_after = stop_after

    @property
    def running(self):
        self._checks += 1
        return self._checks <= self._stop_after


class BlockingPump:
    """Blocks the first pump check so a second request can race the first."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._blocked = False

    @property
    def running(self):
        if not self._blocked:
            self._blocked = True
            self.entered.set()
            self.release.wait(5)
        return True


def make_controller(pump, screen=None, rack=None):
    store = Store()
    recorder = Recorder(store, LogicalClock())
    screen = screen or Screen()
    rack = rack or Rack()
    devices = {"screen": screen, "rack": rack}
    controller = WashController(
        store, recorder, LogicalClock(), pump, devices, default_policies()
    )
    return controller, recorder, screen, rack


# ------------------------------------------------------------------- policy


def test_screen_flushes_only_when_both_signals_trip():
    assert SCREEN_POLICY.evaluate(40.0, 80.0) == (True, ("delta", "flow"))
    assert SCREEN_POLICY.evaluate(40.0, 30.0) == (False, ("delta",))
    assert SCREEN_POLICY.evaluate(10.0, 80.0) == (False, ("flow",))


def test_rack_flushes_when_either_signal_trips():
    assert RACK_POLICY.evaluate(25.0, 10.0) == (True, ("delta",))
    assert RACK_POLICY.evaluate(5.0, 95.0) == (True, ("flow",))
    assert RACK_POLICY.evaluate(5.0, 10.0) == (False, ())


def test_policy_round_trips_through_a_mapping():
    policy = FlushPolicy(DeviceClass.RACK, TriggerMode.ANY, 20.0, 90.0)
    assert FlushPolicy.from_dict(policy.to_dict()) == policy


def test_steps_are_ordered_per_device_class():
    assert steps_for(DeviceClass.SCREEN) == ("isolate", "backwash", "restore")
    assert steps_for(DeviceClass.RACK) == ("rake", "rinse", "restore")


# ----------------------------------------------------------------- decision


def test_decision_is_frozen_at_flush_start():
    controller, _, screen, _ = make_controller(ScriptedPump(10))
    screen.measure(100.0, 60.0)
    decision = controller.evaluate("screen", screen.delta, 80.0)
    screen.measure(100.0, 99.0)  # readings move after the decision
    assert decision.delta == 40.0
    assert decision.flow == 80.0
    with pytest.raises(Exception):
        decision.delta = 1.0


def test_decision_round_trips_through_a_mapping():
    controller, _, screen, _ = make_controller(ScriptedPump(10))
    decision = controller.evaluate("rack", 25.0, 10.0)
    assert FlushDecision.from_dict(decision.to_dict()) == decision


# -------------------------------------------------------------------- runs


def test_screen_run_cleans_and_returns_to_service():
    controller, _, screen, _ = make_controller(ScriptedPump(10))
    screen.measure(100.0, 55.0)
    result = controller.request_flush("screen", screen.delta, 80.0)
    assert result.status == "flushed"
    assert result.record.outcome == "success"
    assert screen.delta == 0.0
    assert screen.isolated is False
    assert [s.status for s in result.record.execution.steps] == ["done"] * 3


def test_pump_stop_is_a_partial_failure_with_a_named_step():
    controller, _, screen, _ = make_controller(ScriptedPump(1))
    screen.measure(100.0, 50.0)
    result = controller.request_flush("screen", screen.delta, 80.0)
    execution = result.record.execution
    assert execution.outcome == "partial"
    assert execution.failed_step == "backwash"
    assert [s.status for s in execution.steps] == ["done", "failed", "skipped"]
    assert execution.reverted is True
    assert execution.position == "idle"
    assert screen.isolated is False  # back in the safe position
    assert screen.delta == 50.0  # the cleaning step never ran


def test_rack_partial_failure_leaves_the_rack_unrinsed():
    controller, _, _, rack = make_controller(ScriptedPump(1))
    result = controller.request_flush("rack", 25.0, 10.0)
    execution = result.record.execution
    assert execution.outcome == "partial"
    assert execution.failed_step == "rinse"
    assert execution.reverted is True
    assert rack.rinsed is False


def test_rack_run_rinses_on_success():
    controller, _, _, rack = make_controller(ScriptedPump(10))
    result = controller.request_flush("rack", 25.0, 10.0)
    assert result.record.outcome == "success"
    assert rack.rinsed is True


def test_not_needed_evaluation_does_not_run_or_persist():
    controller, recorder, _, _ = make_controller(ScriptedPump(10))
    result = controller.request_flush("screen", 5.0, 5.0)
    assert result.status == "not-needed"
    assert result.record is None
    assert controller.sequence == 0
    assert recorder.count() == 0


# -------------------------------------------------------------- idempotency


def test_guard_hands_the_device_to_one_run():
    guard = FlushGuard()
    assert guard.try_acquire("screen", "run-1") is None
    assert guard.try_acquire("screen", "run-2") == "run-1"
    assert guard.current("screen") == "run-1"
    guard.release("screen", "run-2")  # not the owner: a no-op
    assert guard.current("screen") == "run-1"
    guard.release("screen", "run-1")
    assert guard.current("screen") is None


def test_concurrent_requests_flush_once():
    pump = BlockingPump()
    controller, recorder, _, _ = make_controller(pump)
    first = {}
    thread = threading.Thread(
        target=lambda: first.setdefault(
            "result", controller.request_flush("screen", 45.0, 80.0)
        )
    )
    thread.start()
    assert pump.entered.wait(5)
    inflight = controller.inflight("screen")
    assert inflight is not None

    second = controller.request_flush("screen", 45.0, 80.0)
    assert second.status == "duplicate"
    assert second.run_id == inflight

    pump.release.set()
    thread.join(5)
    assert first["result"].status == "flushed"
    assert controller.sequence == 1
    kinds = [event.kind for event in recorder.events()]
    assert kinds.count("flush-decision") == 1
    assert kinds.count("flush-outcome") == 1
    assert kinds.count("flush-duplicate") == 1


class AlwaysRunning:
    running = True


def test_concurrent_flushes_keep_unique_sequences():
    store = Store()
    recorder = Recorder(store, LogicalClock())
    devices = {"screen": Screen(), "rack": Rack()}
    controller = WashController(
        store, recorder, LogicalClock(), AlwaysRunning(), devices, default_policies()
    )
    device_ids = ["screen", "rack"]

    def hammer(index):
        controller.request_flush(device_ids[index % 2], 45.0, 80.0)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    records = controller.records()
    assert controller.sequence == len(records)
    sequences = sorted(
        int(key.rsplit("/", 1)[1]) for key in store.keys(keys.FLUSH_RUN_PREFIX)
    )
    assert sequences == list(range(1, controller.sequence + 1))
    assert controller.verify_all()["consistent"] is True


# --------------------------------------------------------------------- audit


def test_flush_writes_decision_and_outcome_events():
    controller, recorder, screen, _ = make_controller(ScriptedPump(10))
    screen.measure(100.0, 55.0)
    result = controller.request_flush("screen", screen.delta, 80.0)
    decision_event = recorder.latest("flush-decision")
    outcome_event = recorder.latest("flush-outcome")
    assert decision_event is not None
    assert outcome_event is not None
    import json

    decision = json.loads(decision_event.detail)
    outcome = json.loads(outcome_event.detail)
    assert decision["run_id"] == result.run_id
    assert decision["decision_digest"] == decision_digest(result.record.decision)
    assert outcome["record_digest"] == record_digest(result.record)


def test_record_is_the_source_the_audit_references():
    controller, _, screen, _ = make_controller(ScriptedPump(10))
    screen.measure(100.0, 55.0)
    result = controller.request_flush("screen", screen.delta, 80.0)
    stored = controller.record_at(1)
    assert stored == result.record
    assert controller.latest_record("screen") == result.record
    assert record_digest(stored) == record_digest(result.record)


# ------------------------------------------------------- consistency checks


def test_verify_confirms_a_clean_run():
    controller, _, screen, _ = make_controller(ScriptedPump(10))
    screen.measure(100.0, 55.0)
    controller.request_flush("screen", screen.delta, 80.0)
    report = controller.verify_all()
    assert report["consistent"] is True
    assert report["checked"] == 1


def test_verify_catches_a_tampered_record():
    store = Store()
    recorder = Recorder(store, LogicalClock())
    screen = Screen()
    devices = {"screen": screen, "rack": Rack()}
    controller = WashController(
        store, recorder, LogicalClock(), ScriptedPump(10), devices, default_policies()
    )
    screen.measure(100.0, 55.0)
    controller.request_flush("screen", screen.delta, 80.0)
    row = store.get(keys.flush_run_key(1))
    row["decision"]["delta"] = 999.0
    store.put(keys.flush_run_key(1), row)
    report = controller.verify_all()
    assert report["consistent"] is False
    assert report["runs"][0]["consistent"] is False


# ------------------------------------------------------------- console side


def test_console_evaluate_flushes_and_freezes_the_basis(console, plant):
    ok(console, "POST", "/api/start", {})
    plant.screen.measure(100.0, 60.0)  # delta 40, flow is the feed flow 80
    body = ok(console, "POST", "/api/flush/evaluate", {})
    assert body["results"]["screen"]["status"] == "flushed"
    assert body["results"]["rack"]["status"] == "flushed"
    basis = body["results"]["screen"]["record"]["decision"]
    assert basis["delta"] == 40.0
    assert basis["flow"] == 80.0
    assert basis["verdict"] is True


def test_console_shows_the_two_class_trade_offs(console, plant):
    ok(console, "POST", "/api/start", {})
    plant.screen.measure(100.0, 75.0)  # delta 25: screen holds, rack flushes
    body = ok(console, "POST", "/api/flush/evaluate", {})
    assert body["results"]["screen"]["status"] == "not-needed"
    assert body["results"]["rack"]["status"] == "flushed"


def test_console_display_records_and_verify_are_same_source(console, plant):
    ok(console, "POST", "/api/start", {})
    plant.screen.measure(100.0, 60.0)
    ok(console, "POST", "/api/flush/evaluate", {})

    display = ok(console, "GET", "/api/flush")
    feed = ok(console, "GET", "/api/flush/records")
    verify = ok(console, "GET", "/api/flush/verify")

    assert display["consistency"]["consistent"] is True
    assert verify["consistent"] is True
    display_digests = {r["run_id"]: r["record_digest"] for r in display["runs"]}
    feed_digests = {r["run_id"]: r["record_digest"] for r in feed["records"]}
    assert display_digests == feed_digests
    for record in plant.wash.records():
        assert display_digests[record.run_id] == record_digest(record)


def test_records_survive_a_restart(config):
    from support import build

    first = build(config)
    first.plant.start()
    first.plant.screen.measure(100.0, 60.0)
    first.plant.flush_check_all()
    assert first.plant.wash.sequence == 2

    reopened = build(config)
    assert reopened.plant.wash.sequence == 2
    assert reopened.plant.flush_verify()["consistent"] is True
