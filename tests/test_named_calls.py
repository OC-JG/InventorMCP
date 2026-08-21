"""Calling a COM method whose optional arguments sit in the middle.

`RectangularPatternFeatures.Add` takes XSpacingType and XDirectionStartPoint
*between* the X axis and the Y axis. There is no value for either that this
project knows, and putting something there anyway shifts every argument after it
-- which is how a two-axis pattern failed with a bare "Exception occurred" and
nothing in Inventor's error manager to read.
"""

from __future__ import annotations

from inventor_mcp.backend.com.backend import DEFAULTED, _call_named


class Recorder:
    def __init__(self, accepts_keywords: bool = True):
        self.accepts_keywords = accepts_keywords
        self.args: tuple = ()
        self.kwargs: dict = {}

    def __call__(self, *args, **kwargs):
        if kwargs and not self.accepts_keywords:
            raise TypeError("takes no keyword arguments")
        self.args, self.kwargs = args, kwargs
        return "feature"


ARGUMENTS = [
    ("ParentFeatures", "features"),
    ("XDirectionEntity", "x_axis"),
    ("XCount", 4),
    ("XSpacingType", DEFAULTED),
    ("XDirectionStartPoint", DEFAULTED),
    ("YDirectionEntity", "y_axis"),
    ("YCount", 2),
]


def test_named_arguments_omit_the_defaulted_ones():
    """So the wrapper supplies its own, which is the whole point."""
    method = Recorder()
    assert _call_named(method, ARGUMENTS) == "feature"
    assert method.args == ()
    assert method.kwargs == {
        "ParentFeatures": "features", "XDirectionEntity": "x_axis",
        "XCount": 4, "YDirectionEntity": "y_axis", "YCount": 2,
    }
    assert "XSpacingType" not in method.kwargs


def test_the_positional_fallback_keeps_the_gaps():
    """Late binding refuses keywords, and then position is all there is.

    None in the gaps is what a missing optional VARIANT looks like. What must not
    happen is the gaps closing up: that is the bug this exists to prevent.
    """
    method = Recorder(accepts_keywords=False)
    assert _call_named(method, ARGUMENTS) == "feature"
    assert method.args == ("features", "x_axis", 4, None, None, "y_axis", 2)
    assert method.kwargs == {}


def test_the_y_axis_stays_in_its_own_slot():
    """The failure it guards: the second axis landing in XDirectionStartPoint."""
    method = Recorder(accepts_keywords=False)
    _call_named(method, ARGUMENTS)
    assert method.args.index("y_axis") == 5, "one slot per argument, gaps included"


def test_nothing_defaulted_is_just_a_normal_call():
    method = Recorder()
    _call_named(method, [("A", 1), ("B", 2)])
    assert method.kwargs == {"A": 1, "B": 2}
