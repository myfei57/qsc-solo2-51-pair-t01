"""The ordered wash steps for each device class."""

from .policy import DeviceClass

SCREEN_STEPS = ("isolate", "backwash", "restore")
RACK_STEPS = ("rake", "rinse", "restore")


def steps_for(device_class: DeviceClass) -> tuple[str, ...]:
    """The wash program for a device class, in execution order."""
    if device_class is DeviceClass.SCREEN:
        return SCREEN_STEPS
    return RACK_STEPS
