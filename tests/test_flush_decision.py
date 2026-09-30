"""Flush decision: pressure + volume, per-asset policy, idempotency and partial failure."""

import pytest

from desalplant.flush.decision import FlushDecision, FlushDecider
from desalplant.flush.gate import FlushGate, FlushRun
from desalplant.flush.policy import RACK_POLICY, SCREEN_POLICY, FlushPolicy
from desalplant.flush.stage import Stage
from desalplant.flush.volume import VolumeAccumulator

STAGES = (
    Stage(name="pre-rinse", seconds=15),
    Stage(name="chemical-rinse", seconds=20),
    Stage(name="post-rinse", seconds=15),
)


def decide(policy, pressure, volume, pressure_limit=30.0, volume_limit=5000.0, tick=0):
    decider = FlushDecider(pressure_limit, volume_limit, policy)
    return decider.decide("screen", pressure, volume, tick)


# --------------------------------------------------------------------- policy

def test_screen_policy_uses_or():
    # 格栅：压差高或取水量大都冲（宁误冲勿漏冲）。
    assert SCREEN_POLICY.combine == "or"
    assert SCREEN_POLICY.due(pressure_due=True, volume_due=False) is True
    assert SCREEN_POLICY.due(pressure_due=False, volume_due=True) is True
    assert SCREEN_POLICY.due(pressure_due=False, volume_due=False) is False
    assert SCREEN_POLICY.due(pressure_due=True, volume_due=True) is True


def test_rack_policy_uses_and():
    # 机架：压差高且取水量大都满足才冲（宁漏冲勿误冲）。
    assert RACK_POLICY.combine == "and"
    assert RACK_POLICY.due(pressure_due=True, volume_due=False) is False
    assert RACK_POLICY.due(pressure_due=False, volume_due=True) is False
    assert RACK_POLICY.due(pressure_due=False, volume_due=False) is False
    assert RACK_POLICY.due(pressure_due=True, volume_due=True) is True


def test_policy_rejects_unknown_combine():
    with pytest.raises(ValueError):
        FlushPolicy(name="bad", combine="xor").due(True, True)


# ------------------------------------------------------------------- decider

def test_decider_combines_pressure_and_volume():
    # Screen: pressure high alone flushes (OR).
    decision = decide(SCREEN_POLICY, pressure=35.0, volume=100.0)
    assert decision.due is True
    assert decision.pressure_due is True
    assert decision.volume_due is False

    # Screen: volume high alone flushes (OR).
    decision = decide(SCREEN_POLICY, pressure=10.0, volume=6000.0)
    assert decision.due is True
    assert decision.volume_due is True

    # Screen: neither high does not flush.
    decision = decide(SCREEN_POLICY, pressure=10.0, volume=100.0)
    assert decision.due is False


def test_rack_needs_both_signals():
    # Rack: pressure high alone does not flush (AND).
    decision = decide(RACK_POLICY, pressure=35.0, volume=100.0)
    assert decision.due is False
    # Rack: both high flush.
    decision = decide(RACK_POLICY, pressure=35.0, volume=6000.0)
    assert decision.due is True


def test_decision_is_frozen_at_start():
    # The decision captures the readings; later readings do not mutate it.
    decision = decide(SCREEN_POLICY, pressure=35.0, volume=6000.0, tick=5)
    assert decision.tick == 5
    assert decision.pressure_delta == 35.0
    assert decision.intake_volume == 6000.0
    # Frozen dataclass: no attribute may change.
    with pytest.raises(Exception):
        decision.pressure_delta = 0.0


def test_decider_rejects_negative_limits():
    with pytest.raises(ValueError):
        FlushDecider(-1.0, 100.0, SCREEN_POLICY)
    with pytest.raises(ValueError):
        FlushDecider(30.0, -1.0, SCREEN_POLICY)


# ------------------------------------------------------------------- volume

def test_volume_accumulator_tracks_intake():
    volume = VolumeAccumulator()
    assert volume.total == 0.0
    assert volume.accumulate(80.0) == 80.0
    assert volume.accumulate(20.0) == 100.0
    assert volume.reset() == 100.0
    assert volume.total == 0.0


def test_volume_accumulator_rejects_negative_flow():
    with pytest.raises(ValueError):
        VolumeAccumulator().accumulate(-1.0)


# --------------------------------------------------------------------- gate

def make_run(asset="screen", pressure=35.0, volume=6000.0, policy=SCREEN_POLICY):
    decision = decide(policy, pressure, volume)
    return FlushRun(asset=asset, decision=decision, stages=STAGES)


def test_gate_is_idempotent_per_asset():
    # Two concurrent requests for the same asset return the same run.
    gate = FlushGate()
    first = make_run()
    second = make_run()
    assert gate.request(first) is first
    assert gate.request(second) is first  # not second: no double flush
    assert gate.in_flight("screen") is True


def test_gate_allows_different_assets_to_run_together():
    gate = FlushGate()
    screen = make_run(asset="screen")
    rack = make_run(asset="rack", policy=RACK_POLICY)
    assert gate.request(screen) is screen
    assert gate.request(rack) is rack
    assert gate.in_flight("screen") is True
    assert gate.in_flight("rack") is True


def test_gate_drops_a_finished_run():
    gate = FlushGate()
    run = make_run()
    gate.request(run)
    run.step(True)
    run.step(True)
    run.step(True)
    assert run.finished is True
    gate.finish(run)
    assert gate.in_flight("screen") is False


# ------------------------------------------------------------- partial failure

def test_pump_stopping_mid_flush_is_partial_failure():
    run = make_run()
    run.step(True)  # pre-rinse completes
    # Pump stops before the chemical-rinse stage.
    result = run.step(False)
    assert result.completed is False
    assert result.failed_step == "chemical-rinse"
    assert result.safe_position is True
    assert run.safe_position is True
    assert run.failed_step == "chemical-rinse"
    assert run.finished is True


def test_pump_stopping_on_the_first_stage_fails_that_step():
    run = make_run()
    result = run.step(False)
    assert result.failed_step == "pre-rinse"
    assert result.safe_position is True


def test_finished_run_does_not_advance_again():
    run = make_run()
    for _ in STAGES:
        run.step(True)
    assert run.finished is True
    result = run.step(True)
    assert result.already_finished is True
    assert result.completed is True
