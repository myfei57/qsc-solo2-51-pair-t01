"""Flush decision integration: audit basis, idempotency, three-source consistency."""

import pytest

from desalplant.console.plant import Plant
from desalplant.store.keys import flush_decision_key


def test_flush_decision_uses_pressure_and_volume_not_a_timer(plant):
    # No timer: a clean screen with low volume does not flush.
    result = plant.request_flush("screen", pump_running=True)
    assert result["started"] is False
    assert result["decision"]["due"] is False

    # Fouled screen (high pressure) flushes even with low volume (OR policy).
    plant.screen.delta = 35.0
    result = plant.request_flush("screen", pump_running=True)
    assert result["started"] is True
    assert result["decision"]["due"] is True
    assert result["decision"]["pressure_delta"] == 35.0


def test_rack_needs_both_pressure_and_volume(plant):
    # High pressure alone does not flush the rack (AND policy).
    plant.rack.delta = 35.0
    result = plant.request_flush("rack", pump_running=True)
    assert result["started"] is False
    assert result["decision"]["due"] is False

    # Both high flush the rack.
    plant.rack_volume.accumulate(9000.0)
    result = plant.request_flush("rack", pump_running=True)
    assert result["started"] is True


def test_concurrent_flush_requests_are_idempotent(plant):
    plant.screen.delta = 35.0
    first = plant.request_flush("screen", pump_running=True)
    second = plant.request_flush("screen", pump_running=True)
    # Same run: the second request advances the same run, it does not restart.
    assert first["run"]["decision"]["tick"] == second["run"]["decision"]["tick"]
    assert plant.flush_gate.in_flight("screen") is True


def test_decision_is_frozen_at_flush_start(plant):
    plant.screen.delta = 35.0
    first = plant.request_flush("screen", pump_running=True)
    frozen_tick = first["decision"]["tick"]
    frozen_pressure = first["decision"]["pressure_delta"]
    # Mid-flush the readings change; the frozen decision must not change.
    plant.screen.delta = 0.0
    plant.screen_volume.reset()
    second = plant.request_flush("screen", pump_running=True)
    assert second["decision"]["tick"] == frozen_tick
    assert second["decision"]["pressure_delta"] == frozen_pressure


def test_pump_stopping_mid_flush_is_partial_failure(plant):
    plant.screen.delta = 35.0
    result = plant.request_flush("screen", pump_running=True)
    assert result["partial"] is False
    # Pump stops before the next stage.
    result = plant.request_flush("screen", pump_running=False)
    assert result["partial"] is True
    assert result["result"]["failed_step"]
    assert result["result"]["safe_position"] is True
    assert plant.flush_gate.in_flight("screen") is False


def test_decision_basis_is_recorded_in_audit(plant):
    import json

    plant.screen.delta = 35.0
    plant.request_flush("screen", pump_running=True)
    events = [event for event in plant.events() if event.kind == "flush_decision"]
    assert len(events) == 1
    detail = json.loads(events[0].detail)
    assert detail["asset"] == "screen"
    assert detail["pressure_delta"] == 35.0
    assert detail["due"] is True
    assert detail["policy"] == "screen"


def test_three_sources_share_the_same_decision(plant):
    plant.screen.delta = 35.0
    plant.request_flush("screen", pump_running=True)
    # Console status reads the persisted decision.
    status = plant.status()
    assert status["screen_flush"]["pressure_delta"] == 35.0
    # External API reads the same persisted decision.
    persisted = plant.flush_decision("screen")
    assert persisted["pressure_delta"] == 35.0
    # Audit recorded the same basis.
    consistency = plant.flush_consistency("screen")
    assert consistency["consistent"] is True
    assert consistency["persisted"]["pressure_delta"] == 35.0


def test_consistency_detects_a_missing_audit_record(plant):
    plant.screen.delta = 35.0
    plant.request_flush("screen", pump_running=True)
    # Tamper: drop the audit decision so the three sources no longer agree.
    plant.store.put(flush_decision_key("screen"), {"asset": "screen", "pressure_delta": 99.0})
    consistency = plant.flush_consistency("screen")
    assert consistency["consistent"] is False


def test_consistency_is_per_asset(plant):
    # Both assets decide; checking one must not be confused by the other's events.
    plant.screen.delta = 35.0
    plant.request_flush("screen", pump_running=True)
    plant.rack.delta = 35.0
    plant.rack_volume.accumulate(9000.0)
    plant.request_flush("rack", pump_running=True)
    assert plant.flush_consistency("screen")["consistent"] is True
    assert plant.flush_consistency("rack")["consistent"] is True


def test_flush_completion_resets_the_volume(plant):
    plant.screen.delta = 35.0
    plant.screen_volume.accumulate(1000.0)
    for _ in range(3):
        plant.request_flush("screen", pump_running=True)
    assert plant.screen_volume.total == 0.0


def test_flush_audit_routes_answer(console):
    from support import call, ok

    body = ok(console, "GET", "/api/flush/request?asset=screen&pump_running=true")
    assert body["started"] is False
    assert body["decision"]["due"] is False
    consistency = ok(console, "GET", "/api/flush/consistency?asset=screen")
    assert "consistent" in consistency
