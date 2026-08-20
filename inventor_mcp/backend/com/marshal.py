"""Keep every Inventor call on one thread, because COM insists on it.

Inventor's API is apartment-threaded: an interface obtained on one thread may
not be used from another, and ``CoInitialize`` applies per thread rather than
per process.  The MCP SDK runs synchronous tool functions on a pool of worker
threads, so without this the first tool call can land on a thread that never
initialised COM at all -- and a later one on a different thread from the one
that captured the ``Inventor.Application`` reference.

``ipt-mcp`` solves the same problem from inside Inventor, marshalling onto its
STA thread through a hidden message-only window.  From outside we cannot use
Inventor's own message pump, so we own a thread instead: it initialises COM
once, and every call is handed to it and its result handed back.  Calls are
serialised, which is what Inventor wants anyway -- it is a single-user
application driving a single document.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: How long a queued call may wait before we assume the worker is wedged.
DEFAULT_TIMEOUT = 600.0


class ThreadStopped(RuntimeError):
    """Raised when a call is made after the worker has been shut down."""


class SingleThread:
    """Runs every submitted callable on one dedicated worker thread.

    ``setup`` runs on the worker before the first call and ``teardown`` after
    the last, which is where COM initialisation belongs: both have to happen on
    the same thread as the work itself, or they apply to the wrong apartment.
    """

    def __init__(
        self,
        name: str = "inventor-com",
        setup: Callable[[], None] | None = None,
        teardown: Callable[[], None] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._name = name
        self._setup = setup
        self._teardown = teardown
        self._timeout = timeout
        self._work: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started = threading.Event()
        #: Set by stop(). A call must not quietly resurrect the thread: the new
        #: one gets a fresh COM apartment while the backend still holds
        #: interfaces obtained on the old one, which is the exact failure this
        #: class exists to prevent. Only an explicit start() clears it.
        self._shutdown = False
        self.thread_id: int | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            self._shutdown = False
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._serve, name=self._name, daemon=True
            )
            self._thread.start()
        self._started.wait(self._timeout)

    def stop(self) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
            self._shutdown = True
            if thread is None:
                return
        self._work.put(None)
        thread.join(timeout=self._timeout)
        self._started.clear()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- the worker --------------------------------------------------------
    def _serve(self) -> None:
        self.thread_id = threading.get_ident()
        if self._setup is not None:
            try:
                self._setup()
            except Exception:  # pragma: no cover - a broken setup is reported per call
                logger.exception("Setting up the %s thread failed", self._name)
        self._started.set()
        try:
            while True:
                job = self._work.get()
                if job is None:
                    return
                function, args, kwargs, answer = job
                try:
                    answer.put((True, function(*args, **kwargs)))
                except BaseException as exc:  # noqa: BLE001 - relayed to the caller
                    answer.put((False, exc))
        finally:
            if self._teardown is not None:
                try:
                    self._teardown()
                except Exception:  # pragma: no cover - best effort on the way out
                    logger.debug("Tearing down the %s thread failed", self._name)

    # -- calling -----------------------------------------------------------
    def call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run *function* on the worker and return its result here.

        An exception raised on the worker is re-raised to the caller with its
        own type and traceback, so a COM failure still reads as a COM failure
        rather than as a queue error.
        """
        if threading.get_ident() == self.thread_id:
            return function(*args, **kwargs)  # already there; do not deadlock
        if self._shutdown:
            raise ThreadStopped(
                f"The {self._name} thread has been shut down. Reconnect rather "
                "than restarting it: a new thread is a new COM apartment, and "
                "any interface held from the old one is no longer usable."
            )
        self.start()
        if not self.running:
            raise ThreadStopped(f"The {self._name} thread is not running.")
        answer: queue.Queue = queue.Queue(maxsize=1)
        self._work.put((function, args, kwargs, answer))
        try:
            ok, outcome = answer.get(timeout=self._timeout)
        except queue.Empty as empty:  # pragma: no cover - a wedged Inventor
            raise TimeoutError(
                f"Inventor did not answer within {self._timeout:.0f}s. It may be "
                "showing a dialog that needs a click."
            ) from empty
        if ok:
            return outcome
        raise outcome


def on_thread(target: Any, thread: SingleThread) -> Any:
    """*target* with every public method routed through *thread*.

    A proxy rather than a decorator on each method: a backend has a few dozen
    of them and forgetting one would leave a call on the wrong apartment, which
    fails in a way that points at Inventor rather than at the omission.
    """

    class Marshalled:
        #: So callers can reach the real object, and tests can prove they are
        #: not accidentally talking to the proxy.
        unmarshalled = target
        marshalling_thread = thread

        def __getattr__(self, name: str) -> Any:
            attribute = getattr(target, name)
            if not callable(attribute) or name.startswith("__"):
                return attribute

            def marshalled(*args: Any, **kwargs: Any) -> Any:
                return thread.call(attribute, *args, **kwargs)

            marshalled.__name__ = getattr(attribute, "__name__", name)
            marshalled.__doc__ = getattr(attribute, "__doc__", None)
            return marshalled

        def __repr__(self) -> str:  # pragma: no cover - debugging aid
            return f"<{target!r} on {thread._name}>"

    return Marshalled()
