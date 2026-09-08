"""Getting onto the thread that owns Inventor's objects, for a probe.

Inventor's API is apartment-threaded: an interface obtained on one thread may
not be used from another. ``inventor_mcp/backend/com/marshal.py`` handles that
for the server by pinning the backend to one worker thread and routing every
public method onto it -- so a *tool* never has to think about this, because a
tool only ever gets plain data back.

A probe is the exception, and the reason this module exists. A probe's whole job
is to hold a live COM object and ask it what it offers, and the proxy hands the
object back across the thread boundary where it is already dead. Two symptoms,
both measured on 2026-09-08 and neither of them pointing at the cause:

* ``document.ComponentDefinition`` raises *"the application called an interface
  that was marshalled for a different thread"* -- the honest error;
* ``app.FileManager`` raises a bare ``AttributeError: <unknown>.FileManager``,
  which reads exactly like a property this release does not have. It has it.

So the work has to *run* on the apartment rather than be handed its objects.
``backend/base.py``'s ``describe_feature`` says the same thing for the server
side: "reading the properties *there* and returning numbers is the only way to
ask what Inventor actually built."

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
    """
    worker = getattr(backend, "marshalling_thread", None)
    return worker.call(work) if worker is not None else work()
