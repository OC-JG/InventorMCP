"""Backend implementations and the contract they share."""

from .base import Backend

__all__ = ["Backend", "create_backend"]


def create_backend(kind: str = "auto"):
    """Build a backend.

    ``auto`` uses Inventor over COM when it is importable and falls back to the
    mock backend everywhere else, so the server always starts.
    """
    from ..errors import BackendUnavailableError

    if kind in ("auto", "com", "inventor"):
        try:
            from .com.backend import ComBackend

            return _pinned(ComBackend())
        except BackendUnavailableError:
            if kind != "auto":
                raise
    if kind in ("auto", "mock", "simulated"):
        from .mock.backend import MockBackend

        return MockBackend()
    raise BackendUnavailableError(
        f"Unknown backend {kind!r}.", hint="Use 'auto', 'inventor' or 'mock'."
    )


def _pinned(backend):
    """The COM backend with every call pinned to one thread.

    Inventor's API is apartment-threaded and ``CoInitialize`` is per thread,
    while the MCP SDK runs synchronous tools on a pool of worker threads. Left
    alone, the first tool call can land on a thread that never initialised COM,
    and a later one on a different thread from the one holding the
    ``Inventor.Application`` reference.

    Set ``INVENTOR_MCP_THREADING=off`` to talk to Inventor directly from
    whichever thread calls -- useful for isolating a problem, not for running.
    """
    import os

    if os.environ.get("INVENTOR_MCP_THREADING", "").lower() in ("off", "0", "none"):
        return backend

    from .com.marshal import SingleThread, on_thread

    def initialise() -> None:
        try:
            import pythoncom  # type: ignore[import-not-found]

            pythoncom.CoInitialize()
        except Exception:  # pragma: no cover - Windows only
            pass

    def finalise() -> None:
        try:
            import pythoncom  # type: ignore[import-not-found]

            pythoncom.CoUninitialize()
        except Exception:  # pragma: no cover - Windows only
            pass

    return on_thread(backend, SingleThread("inventor-com", initialise, finalise))
