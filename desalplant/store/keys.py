"""Key layout of the persisted control state."""

PLANT_STATE = "plant/state"
PLANT_STARTED_AT = "plant/started_at"
PLANT_STOPPED_AT = "plant/stopped_at"
PLANT_MAINTAINED_AT = "plant/maintained_at"

AUDIT_SEQUENCE = "audit/sequence"
AUDIT_EVENT_PREFIX = "audit/event/"
AUDIT_LAST_PREFIX = "audit/last/"

FLUSH_SEQUENCE = "flush/sequence"
FLUSH_RUN_PREFIX = "flush/run/"
FLUSH_LATEST_PREFIX = "flush/latest/"


def audit_event_key(sequence: int) -> str:
    """Key holding the event written at ``sequence``."""
    if sequence < 1:
        raise ValueError("audit sequence starts at one")
    return f"{AUDIT_EVENT_PREFIX}{sequence:06d}"


def audit_last_key(kind: str) -> str:
    """Key holding the sequence of the latest event of ``kind``."""
    return f"{AUDIT_LAST_PREFIX}{kind}"


def flush_run_key(sequence: int) -> str:
    """Key holding the flush record written at ``sequence``."""
    if sequence < 1:
        raise ValueError("flush sequence starts at one")
    return f"{FLUSH_RUN_PREFIX}{sequence:06d}"


def flush_latest_key(device_id: str) -> str:
    """Key holding the sequence of a device's latest flush record."""
    return f"{FLUSH_LATEST_PREFIX}{device_id}"
