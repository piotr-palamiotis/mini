"""What parallel sessions share, process-wide.

LOCAL: the local model serves one reply at a time (llama-server has one slot). Replies from different sessions take
turns in arrival order; llama-server swaps the context itself (its prompt cache, models loaded on demand), mini
holds the model for one reply only. Cloud tiers are not gated.

FOLDER: file changes in the user's folder (and their git checkpoint) happen one at a time.

Both take on_wait(): called once, only when the caller has to wait, and stop_event: set while waiting, the wait ends
with Stopped so an aborted session does not keep its place in the line.
"""
import collections
import contextlib
import threading


class Stopped(Exception):
    """The caller was stopped while waiting for a gate."""


class Gate:
    """A FIFO lock: the longest waiter goes first. Re-entrant for the thread that holds it."""

    def __init__(self):
        self._cv = threading.Condition()
        self._line = collections.deque()
        self._owner, self._depth = None, 0

    @property
    def busy(self):
        return self._owner is not None

    @contextlib.contextmanager
    def hold(self, on_wait=None, stop_event=None):
        me = threading.get_ident()
        with self._cv:
            if self._owner == me:
                self._depth += 1
            else:
                ticket = object()
                self._line.append(ticket)
                waited = False
                while self._owner is not None or self._line[0] is not ticket:
                    if stop_event is not None and stop_event.is_set():
                        self._line.remove(ticket)
                        self._cv.notify_all()
                        raise Stopped()
                    if not waited and on_wait:
                        waited = True
                        self._cv.release()
                        try:
                            on_wait()
                        finally:
                            self._cv.acquire()
                        continue
                    self._cv.wait(0.2)
                self._line.popleft()
                self._owner, self._depth = me, 1
        try:
            yield
        finally:
            with self._cv:
                self._depth -= 1
                if not self._depth:
                    self._owner = None
                    self._cv.notify_all()


LOCAL = Gate()
FOLDER = Gate()
