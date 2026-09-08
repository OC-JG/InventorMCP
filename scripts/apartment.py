"""Getting onto the thread that owns Inventor's live objects, for a probe.

The COM backend is pinned to one thread. ``_pinned`` in
``inventor_mcp/backend/__init__.py`` wraps it in the ``on_thread`` proxy from
``inventor_mcp/backend/com/marshal.py``, which routes every method onto one
dedicated worker thread, because Inventor's API is apartment-threaded: an
interface obtained on one thread may not be used from another. So a *tool*
never has to think about this, because a tool only ever gets plain data back.

A probe is the exception, and the reason this module exists. Its whole job is to
hold a live COM object and ask what it offers, and the proxy hands that object
back across the boundary where it is already dead. Two symptoms, both measured
on 2026-09-08 and neither of them pointing at the cause:

* ``document.ComponentDefinition`` raises *"the application called an interface
  that was marshalled for a different thread"* -- the honest error;
* ``app.FileManager`` raises a bare ``AttributeError: <unknown>.FileManager``,
  which reads exactly like a property this release does not have. It has it.

So a probe does all of its live-object work inside one callable handed to
:func:`on_thread`, printing from in there or returning plain data. Only text and
numbers cross back. ``describe_feature`` in ``inventor_mcp/backend/base.py``
states the same rule for the backend's own callers: "reading the properties
*there* and returning numbers is the only way to ask what Inventor actually
built."

Both helpers are here rather than copied into each probe because there were two
copies already and a third was about to be written, and a probe that forgets one
of them fails in a way that blames Inventor.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

T = TypeVar("T")


def raw(backend: Any) -> Any:
    """The backend itself, behind the marshalling proxy.

    ``getattr`` with a fallback rather than an attribute access, so a probe runs
    unchanged against a backend that was never wrapped -- the mock, or a COM
    backend built directly in a test.
    """
    return getattr(backend, "unmarshalled", backend)


def on_thread(backend: Any, work: Callable[[], T]) -> T:
    """Run *work* on the apartment that owns Inventor's objects.

    Everything *work* touches must be reached from inside it. Passing a live
    object in is the same mistake as taking one out, and it fails identically.

    A backend with no proxy -- the simulator -- runs it here, which is the same
    thing: there is no other apartment to be on.
    """
    worker = getattr(backend, "marshalling_thread", None)
    return worker.call(work) if worker is not None else work()


def _demo() -> None:
    """Prove both helpers against the real proxy, with no Inventor involved.

    The probes cannot be run off a Windows machine with Inventor, so this checks
    the one thing that is checkable here: that ``on_thread`` really lands on the
    worker and ``raw`` really gets past the proxy.
    """
    import sys
    import threading
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from inventor_mcp.backend.com.marshal import SingleThread
    from inventor_mcp.backend.com.marshal import on_thread as pin

    class Fake:
        def where(self) -> int:
            return threading.get_ident()

    real = Fake()
    thread = SingleThread("apartment-demo")
    proxy = pin(real, thread)
    try:
        assert raw(proxy) is real, "raw() did not get past the proxy"
        assert raw(real) is real, "raw() should pass an unpinned backend through"
        apartment = proxy.where()
        assert apartment != threading.get_ident(), "the proxy did not marshal"
        assert on_thread(proxy, real.where) == apartment, "work ran on the wrong thread"
        assert on_thread(real, real.where) == threading.get_ident(), \
            "an unpinned backend has no other apartment to be on"
    finally:
        thread.stop()
    print("apartment: ok")


if __name__ == "__main__":
    _demo()
