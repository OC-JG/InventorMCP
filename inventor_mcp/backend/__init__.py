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

            return ComBackend()
        except BackendUnavailableError:
            if kind != "auto":
                raise
    if kind in ("auto", "mock", "simulated"):
        from .mock.backend import MockBackend

        return MockBackend()
    raise BackendUnavailableError(
        f"Unknown backend {kind!r}.", hint="Use 'auto', 'inventor' or 'mock'."
    )
