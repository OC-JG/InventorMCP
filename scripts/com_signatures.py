"""Print the real COM signatures pywin32 generated for Inventor's methods.

Argument order and type are not guessable, and getting them wrong produces
errors that name neither -- "Type mismatch", or an int conversion failing on a
string that was meant for a different parameter entirely. The generated
wrapper knows the truth, so read it rather than guess.

    python scripts/com_signatures.py                     # the methods this server calls
    python scripts/com_signatures.py HoleFeatures        # everything on one class
    python scripts/com_signatures.py HoleFeatures.AddDrilledByDistanceExtent
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The calls whose argument order this server depends on.
INTERESTING = [
    "HoleFeatures.AddDrilledByDistanceExtent",
    "HoleFeatures.AddDrilledByThroughAllExtent",
    "ShellFeatures.CreateShellDefinition",
    "MirrorFeatures.Add",
    "FilletFeatures.AddSimple",
    "ChamferFeatures.AddUsingDistance",
    "RectangularPatternFeatures.Add",
    "CircularPatternFeatures.Add",
    "Profiles.AddForSolid",
    "ThreadFeatures.CreateThreadDefinition",
    "RevolveFeatures.AddByAngle",
    "RevolveFeatures.AddFull",
    "SweepFeatures.AddUsingPath",
    # Added after the first live acceptance run, which failed on each of these:
    # the seats all came out shallower than asked, the loft and the sweep raised
    # a bare "Exception occurred", and ThreadFeatures turned out not to have
    # CreateThreadDefinition on 2027.1 at all.
    "HoleFeatures.AddCBoreByThroughAllExtent",
    "HoleFeatures.AddCBoreByDistanceExtent",
    "HoleFeatures.AddSpotFaceByThroughAllExtent",
    "HoleFeatures.AddCSinkByThroughAllExtent",
    "HoleFeatures.CreateTapInfo",
    "HoleFeatures.CreateSketchPlacementDefinition",
    "PartFeatures.CreatePath",
    "ThreadFeatures.Add",
    "LoftFeatures.CreateLoftDefinition",
    "LoftFeatures.Add",
    "SweepFeatures.Add",
    "SweepFeatures.CreateSweepDefinition",
    "WorkPlanes.AddByPlaneAndOffset",
    "UserParameters.AddByExpression",
    "UserParameters.AddByValue",
]

#: VARIANT type codes that appear in generated InvokeTypes calls.
VARTYPE = {
    3: "int (VT_I4 - an enum, not a flag)", 5: "float", 8: "string",
    9: "COM object", 11: "bool", 12: "variant (number or expression string)",
    16: "signed char", 17: "byte",
}


def generated_root() -> Path:
    import win32com.client
    from win32com.client import gencache

    try:
        win32com.client.gencache.EnsureDispatch("Inventor.Application")
    except Exception as exc:
        print(f"Could not reach Inventor to generate the wrapper: {exc}", file=sys.stderr)
    root = Path(gencache.GetGeneratePath())
    candidates = [d for d in root.iterdir() if d.is_dir() and (d / "HoleFeatures.py").exists()]
    if not candidates:
        raise SystemExit(f"No Inventor type-library cache found under {root}")
    return max(candidates, key=lambda d: d.stat().st_mtime)


#: The tuple of (vartype, flags) pairs InvokeTypes passes for the arguments.
#: The return spec just before it is a single pair, so a run of two or more
#: never matches it by accident.
_ARG_TYPES = re.compile(r"\(\s*((?:\(\d+,\s*\d+\)\s*,?\s*){2,})\)")
_ONE_TYPE = re.compile(r"\((\d+),\s*(\d+)\)")

#: PARAMFLAG bits worth reporting.
_OPTIONAL = 0x10


#: Generated modules list every readable property in a _prop_map_get_ dict.
_PROP_MAP = re.compile(r"_prop_map_get_ = \{(?P<body>.*?)\n\t\}", re.DOTALL)
_PROP_NAME = re.compile(r'"(\w+)"\s*:')


def properties(path: Path) -> list[str]:
    """The class's readable properties, which have no signature to print.

    Worth listing separately: a method that is missing may just be a property,
    and looking only at methods once made `EdgeUse` look absent entirely.
    """
    match = _PROP_MAP.search(path.read_text(errors="replace"))
    return sorted(set(_PROP_NAME.findall(match.group("body")))) if match else []


def describe(path: Path, method: str | None) -> None:
    source = path.read_text(errors="replace")
    name_pattern = method or r"\w+"
    # Parameter lists wrap across lines once a method takes more than a few,
    # so the parameter group has to span newlines.
    pattern = re.compile(
        r"\n\tdef (?P<name>" + name_pattern + r")\((?P<params>.*?)\):\n"
        r"(?P<body>(?:\t\t.*\n)+)",
        re.DOTALL,
    )
    found = False
    for match in pattern.finditer(source):
        body = match.group("body")
        types_match = _ARG_TYPES.search(body)
        params = [
            part.split("=")[0].strip()
            for part in match.group("params").replace("\n", " ").split(",")[1:]
            if part.strip()
        ]
        if not params and not types_match:
            continue
        found = True
        types = _ONE_TYPE.findall(types_match.group(1)) if types_match else []
        print(f"\n{path.stem}.{match.group('name')}")
        if not params:
            print("    (no arguments)")
        for index, name in enumerate(params):
            if index < len(types):
                vartype, flags = int(types[index][0]), int(types[index][1])
                kind = VARTYPE.get(vartype, f"vartype {vartype}")
                if flags & _OPTIONAL:
                    kind += ", optional"
            else:
                kind = "?"
            print(f"    {index}. {name:28} {kind}")
    if method and not found:
        print(f"\n{path.stem}.{method}: not a method")
        if method in properties(path):
            print("    it is a property -- read it, do not call it")
    if not method:
        readable = properties(path)
        if readable:
            print(f"\n{path.stem} properties:")
            for index in range(0, len(readable), 4):
                print("    " + "  ".join(f"{name:26}" for name in readable[index:index + 4]))
        elif not found:
            print(f"\n{path.stem}: no methods and no properties in the wrapper")


def main(argv: list[str]) -> int:
    root = generated_root()
    print(f"Type-library cache: {root}")
    targets = argv[1:] or INTERESTING
    for target in targets:
        class_name, _, method = target.partition(".")
        path = root / f"{class_name}.py"
        if not path.exists():
            matches = sorted(root.glob(f"{class_name}*.py"))
            if not matches:
                print(f"\n{class_name}: no generated module in this type library.")
                print("    makepy writes a module per interface it generates, and skips "
                      "some.\n    That is not proof the interface is missing: late binding "
                      "asks the\n    object, not the wrapper. Probe a live one -- see "
                      "scripts/probe_convexity.py.")
                continue
            path = matches[0]
            print(f"\n({class_name} found as {path.stem})")
        describe(path, method or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
