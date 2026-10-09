"""Hardware watchdog: abort the run when swap use grows past the threshold (an earlier run died to the OOM killer)."""
import threading


def swap_used_mib():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
    return (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) / 1024


class SwapWatch:
    """Polls swap use; calls on_trip(growth_mib) once when growth since start exceeds limit_mib."""

    def __init__(self, limit_mib, on_trip, interval_s=2.0, reader=swap_used_mib):
        self.limit, self.on_trip, self.interval, self.read = limit_mib, on_trip, interval_s, reader
        self.start = self.read()
        self.tripped = False
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def growth(self):
        return self.read() - self.start

    def check(self):
        g = self.growth()
        if not self.tripped and g > self.limit:
            self.tripped = True
            self.on_trip(round(g, 1))
        return g

    def _loop(self):
        while not self._stop.wait(self.interval):
            self.check()

    def begin(self):
        self._t.start()
        return self

    def stop(self):
        self._stop.set()
