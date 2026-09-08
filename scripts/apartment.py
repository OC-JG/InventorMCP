"""Getting at Inventor's live objects from a probe script.

The COM backend is pinned to one thread. ``_pinned`` in
``inventor_mcp/backend/__init__.py`` wraps it in the ``on_thread`` proxy from
``inventor_mcp/backend/com/marshal.py``, which routes every method onto one
dedicated worker thread, because Inventor's API is apartment-threaded: an
interface obtained on one thread may not be used from another.

So a live COM object handed back across that proxy is already dead. Touching it
raises either "the application called an interface that was marshalled for a
different thread" or -- worse, because it reads like a property this release
does not have -- a bare ``AttributeError: <unknown>.SomeProperty``.

A probe therefore does all of its live-object work inside one callable handed
to :func:`on_thread`, printing from in there or returning plain data. Only text
and numbers cross back. ``describe_feature`` in ``inventor_mcp/backend/base.py``
states the same rule for the backend's own callers.
"""

from __future__ import annotations


def raw(backend):
    """The backend itself, behind the marshalling proxy."""
    return getattr(backend, "unmarshalled", backend)


def on_thread(backend, work):
    """Run *work* on the apartment that owns Inventor's objects.

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
