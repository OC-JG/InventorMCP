"""Which module owns what, and the promise that moving it broke nothing.

`builder.py` reached 1,200 lines doing five jobs -- resolution, dispatch, static
checking, rehearsal, divergence -- with `_apply_one` alone at 238 of them. It was
not broken. It was where drawings and assemblies are going to land, and it should
be three files before that happens rather than after.

So `checks.py` is what can be said about a recipe without building anything, and
`rehearsal.py` is building it in the simulator and holding a live build up
against that. Both were moved verbatim.

Three things have to stay true and none of them is obvious from reading one
file, which is why they are here rather than in a comment.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Every name that used to live in `builder` and now does not. Callers imported
#: these for the whole of that module's life, so they still resolve from it.
MOVED = {
    "check_recipe": "checks",
    "_undriven_parameters": "checks",
    "compare_to_rehearsal": "rehearsal",
    "rehearse": "rehearsal",
    "PREDICTED": "rehearsal",
    "NOTICEABLE": "rehearsal",
    "TOUCHING": "rehearsal",
    "_KNOWN_BROKEN": "rehearsal",
    "_NOT_MODELLED": "rehearsal",
    "_SUBTRACTIVE": "rehearsal",
    "_MUST_MOVE": "rehearsal",
    "_MEASURES_MATERIAL": "rehearsal",
    "_profile_reaches_the_part": "rehearsal",
    "_removes_material": "rehearsal",
    "_sketches_that_cut": "rehearsal",
    "_warn_about": "rehearsal",
    "_divergence_reason": "rehearsal",
}


def top_level_imports(module: str) -> set[str]:
    """Modules imported at the top of a file, not inside a function."""
    tree = ast.parse((ROOT / "inventor_mcp" / f"{module}.py").read_text())
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


class TestNothingWasBrokenByMovingIt:
    @pytest.mark.parametrize("name,module", sorted(MOVED.items()))
    def test_it_still_resolves_from_builder_and_is_the_same_object(self, name, module):
        import importlib

        from inventor_mcp import builder

        owner = importlib.import_module(f"inventor_mcp.{module}")
        assert getattr(builder, name) is getattr(owner, name)

    def test_the_list_in_the_code_is_the_list_here(self):
        """Otherwise a name could be dropped from the shim and nothing would say."""
        from inventor_mcp.builder import _MOVED

        assert _MOVED == MOVED

    def test_a_name_that_never_lived_there_is_still_an_error(self):
        from inventor_mcp import builder

        with pytest.raises(AttributeError):
            builder.no_such_thing

    def test_the_shim_is_a_shim_and_not_a_second_definition(self):
        """`builder` must not define them again; it forwards, once, on demand."""
        from inventor_mcp import builder

        tree = ast.parse((ROOT / "inventor_mcp/builder.py").read_text())
        defined = {node.name for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        defined |= {target.id for node in tree.body if isinstance(node, ast.Assign)
                    for target in node.targets if isinstance(target, ast.Name)}
        assert not (defined & set(MOVED)), "moved names are defined in builder again"
        assert "rehearse" not in vars(builder), "the forward should not be cached"


class TestTheDependenciesRunOneWay:
    """`rehearsal` -> `checks` -> `builder`, and nothing back except inside a call.

    A build has to compare itself against a rehearsal, so `build_part` imports
    `rehearsal` -- inside the function, which is the one place a circle is
    broken deliberately. If that import climbs to the top of the file, importing
    either module first stops working, and which one gets imported first depends
    on the caller.
    """

    def test_builder_does_not_import_its_neighbours_at_the_top(self):
        assert not ({".checks", ".rehearsal", "checks", "rehearsal"}
                    & top_level_imports("builder"))

    def test_checks_needs_no_backend(self):
        """The point of the file: these are the checks that cost nothing to run."""
        imports = top_level_imports("checks")
        assert not any(name.startswith(".backend") or name == "backend"
                       for name in imports)

    @pytest.mark.parametrize("first", ["builder", "checks", "rehearsal"])
    def test_importing_any_of_them_first_works(self, first):
        """In a fresh interpreter, because import order is only wrong once."""
        result = subprocess.run(
            [sys.executable, "-c", f"import inventor_mcp.{first}"],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert result.returncode == 0, result.stderr


class TestTheStaticChecksReallyAreStatic:
    def test_check_recipe_runs_with_no_session_and_no_backend(self):
        from inventor_mcp.checks import check_recipe
        from inventor_mcp.schema import PartRecipe

        report = check_recipe(PartRecipe.model_validate({
            "name": "Plate", "units": "mm",
            "parameters": [{"name": "w", "value": 40}],
            "operations": [
                {"op": "sketch", "name": "O", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": "w", "height": 20}]},
                {"op": "extrude", "sketch": "O", "distance": 5},
            ]}))
        assert report["ok"], report["findings"]
        assert report["parameters"] == {"w": pytest.approx(4.0)}
