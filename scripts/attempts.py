"""Try things against a live API, print each result, and let none stop the rest.

Both probes that reach for undocumented routes need the same thing: make a call,
say what happened, carry on to the next one regardless. The first version of the
sweep probe collected its answers and printed them at the end, so an exception
outside the guarded calls threw away everything it had learned -- which is what
happened: it died on a call that was not wrapped and reported none of the three
it had already made. Printing as it goes is the whole point.
"""

from __future__ import annotations


def describe(value) -> str:
    """One outcome in as few words as identify it.

    Tried in order because the useful name depends on what came back: a document
    has a ``FullFileName``, a feature a ``Name``, an iProperty a ``Value``. A
    plain type name is the last resort, and says less than any of them.
    """
    for attribute in ("FullFileName", "DisplayName", "Name", "Value"):
        try:
            found = getattr(value, attribute)
        except Exception:
            continue
        if found:
            return f"{type(value).__name__}: {found}"
    if isinstance(value, (str, int, float, bool)):
        return repr(value)
    return type(value).__name__


class Attempts:
    """Call ``attempts(label, work)`` per route; it returns the outcome or None.

    *explain* turns an exception into one readable line -- the backend's own
    ``_explain``, so a COM failure reads the way it does everywhere else.
    """

    def __init__(self, explain):
        self._explain = explain

    def __call__(self, label: str, work):
        try:
            outcome = work()
        except Exception as exc:
            print(f"  refused {label}\n            {self._explain(exc)}")
            return None
        print(f"  ok      {label}\n            {describe(outcome)}")
        return outcome

    def undo(self, feature) -> None:
        """Delete what an attempt made, so the next one starts from the same part."""
        if feature is None:
            return
        try:
            feature.Delete()
        except Exception as exc:  # pragma: no cover
            print(f"            (could not undo it: {str(exc)[:70]})")


def _demo() -> None:
    """The three cases that matter: a value, a failure, and something with a name."""
    class Feature:
        Name = "Sweep1"
        deleted = False

        def Delete(self):
            Feature.deleted = True

    attempts = Attempts(lambda exc: f"{type(exc).__name__}: {exc}")
    assert attempts("a value", lambda: "hello") == "hello"
    assert attempts("a failure", lambda: 1 / 0) is None, "a refusal must return None"
    made = attempts("a feature", Feature)
    assert made is not None and made.Name == "Sweep1"
    attempts.undo(made)
    assert Feature.deleted, "undo() did not delete it"
    attempts.undo(None)  # must not raise
    assert describe(3) == "3" and describe(Feature()) == "Feature: Sweep1"
    print("attempts: ok")


if __name__ == "__main__":
    _demo()
