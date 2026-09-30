"""Per-device guard that makes concurrent flush requests idempotent."""

import threading


class FlushGuard:
    """Tracks the in-flight run per device so a device flushes once at a time.

    The console serves requests on a thread pool, so two flush requests for
    the same device can race. The guard hands the device to the first request
    and tells every later request which run is already in flight, instead of
    starting a second flush.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[str, str] = {}

    def try_acquire(self, device_id: str, run_id: str) -> str | None:
        """Claim ``device_id`` for ``run_id``.

        Returns ``None`` when the claim was taken, or the run id already in
        flight for the device.
        """
        with self._lock:
            current = self._inflight.get(device_id)
            if current is not None:
                return current
            self._inflight[device_id] = run_id
            return None

    def release(self, device_id: str, run_id: str) -> None:
        """Drop the claim, but only when it is still ours."""
        with self._lock:
            if self._inflight.get(device_id) == run_id:
                del self._inflight[device_id]

    def current(self, device_id: str) -> str | None:
        """The in-flight run id for a device, or ``None`` when it is idle."""
        with self._lock:
            return self._inflight.get(device_id)
