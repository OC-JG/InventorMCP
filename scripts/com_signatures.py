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


_INVOKE = re.compile(r"InvokeTypes\([^,]+,[^,]+,[^,]+,\s*\([^)]*\),\s*\((?P<types>.*?)\),(?P<args>.*)",
                     re.DOTALL)


def describe(path: Path, method: str | None) -> None:
    source = path.read_text(errors="replace")
    name_pattern = method or "\\w+"
    pattern = re.compile(
        "\tdef (?P<name>" + name_pattern + ")\((?P<params>.*?)\):\n(?P<body>(?:\t\t.*\n)+)"
    )
    found = False
    for match in pattern.finditer(source):
        invoke = _INVOKE.search(match.group("body"))
        if not invoke:
            continue
        found = True
        params = [p.split("=")[0].strip() for p in match.group("params").split(",")[1:]]
        types = [int(t) for t in re.findall(r"\((\d+),\s*\d+\)", invoke.group("types"))]
        print(f"\n{path.stem}.{match.group('name')}")
        for index, name in enumerate(params):
            kind = VARTYPE.get(types[index], f"type {types[index]}") if index < len(types) else "?"
            print(f"    {index}. {name:28} {kind}")
    if method and not found:
        print(f"\n{path.stem}.{method}: not found")


def main(argv: list[str]) -> int:
    root = generated_root()
    print(f"Type-library cache: {root}")
    targets = argv[1:] or INTERESTING
    for target in targets:
        class_name, _, method = target.partition(".")
        path = root / f"{class_name}.py"
        if not path.exists():
            print(f"\n{class_name}: no generated module")
            continue
        describe(path, method or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
