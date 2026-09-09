"""A frozen parameter is visible in Inventor, and its graph has two sources.

Two small things the published reference made available, and they are related:
both are about a freeze being *legible* rather than only enforced.

`Parameter.IsKey` is a documented read-write flag, and it is what puts a
parameter at the top of Inventor's parameters dialog with a tick beside it. A
freeze otherwise lives in a sidecar file and a property set inside the part --
places somebody would have to know to look. So a parameter this server refuses
to change is marked key as well, and the protection is still the refusal.

`Parameter.DrivenBy` and `Parameter.Dependents` are Inventor's own dependency
graph. Everything the freeze guard knows about dependencies it reconstructs by
*parsing expressions*, so Inventor's answer is a second source against the
parser rather than a replacement for it -- the parser has to stay, because it
works on the simulator and is what `rehearse` uses before any seat is
involved. A disagreement is reported, both ways round, with what each direction
would mean.
"""

from __future__ import annotations

import inspect

from inventor_mcp.backend.com import backend as com
from inventor_mcp import builder


class TestAFrozenParameterIsMarkedKey:
    def test_the_freeze_sets_the_flag(self):
        source = inspect.getsource(builder.apply_parameter)
        assert "key=spec.key or frozen_here" in source
        assert "context.frozen.check(spec.name) is not None" in source

    def test_a_recipe_can_still_ask_for_key_on_its_own(self):
        """The two are independent: `key: true` on a parameter nobody froze
        still marks it, and a freeze marks one that did not ask."""
        source = inspect.getsource(builder.apply_parameter)
        assert "spec.key or" in source, "the recipe's own flag is not dropped"

    def test_the_refusal_is_still_what_protects_it(self):
        """The flag is visibility, not enforcement. If this ever became the
        protection, a parameter would be protected only in a dialog."""
        source = inspect.getsource(builder.apply_parameter)
        assert source.index("context.frozen.refuse(spec.name)") < \
            source.index("frozen_here")


class TestInventorsGraphIsASecondSource:
    def test_it_is_read_and_reported_beside_the_parser(self):
        source = inspect.getsource(com.ComBackend.feature_dependencies)
        assert '"inventors_graph"' in source
        assert '"graph_disagrees"' in source

    def test_it_reads_both_directions(self):
        """`DrivenBy` is what a parameter is computed from and `Dependents`
        what is computed from it. A freeze cares about both: the first says
        what moving this would break, the second what breaking it would move."""
        source = inspect.getsource(com.ComBackend._parameter_graph)
        assert '"DrivenBy", "Dependents"' in source

    def test_a_release_that_will_not_answer_is_not_read_as_agreement(self):
        """None where nothing answered, and the caller then reports no graph at
        all -- which is the difference between "Inventor says nothing depends
        on this" and "Inventor was not asked"."""
        source = inspect.getsource(com.ComBackend._parameter_graph)
        assert "return found if answered else None" in source
        caller = inspect.getsource(com.ComBackend.feature_dependencies)
        assert "if graph is not None:" in caller

    def test_the_parser_stays_the_answer(self):
        """`parameters` and `via` are what every caller reads, and the graph is
        added beside them. It has to be this way round: the parser works on the
        simulator, and `rehearse` runs before any seat is involved."""
        source = inspect.getsource(com.ComBackend.feature_dependencies)
        answer = source[source.index('answer: dict[str, Any] = {'):]
        assert '"parameters": sorted(via' in answer.split("}")[0] + "}"
