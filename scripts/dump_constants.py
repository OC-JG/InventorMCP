"""Print the enum value Inventor itself gives for every constant this uses.

The fallback table in `inventor_mcp/backend/com/constants.py` has never been
exercised: on every machine this has run on the type library was readable and
won, so the table was never consulted and never checked. Two of its entries are
now contradicted by another project's published field notes, and a wrong
enum here is the quiet kind of wrong -- an aligned dimension where a horizontal
one was meant, a cut that joins.

This asks Inventor and prints a corrected table, so the entries stop being
beliefs. Run it once; paste the result.

    python scripts/dump_constants.py
    python scripts/dump_constants.py --only Dim Surface
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inventor_mcp.backend.com.constants import FALLBACK, SUSPECT, load  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", default=[],
                        help="Substrings to filter names by, e.g. Dim Surface.")
    parser.add_argument("--find", nargs="*", default=[], metavar="SUBSTRING",
                        help="List every enum in the type library whose name "
                             "contains one of these, whether the table knows it or "
                             "not. Use it when a value is needed and there is "
                             "nothing to compare against -- 'Health' settles what "
                             "a feature's HealthStatus means.")
    args = parser.parse_args(argv)

    constants = load()
    if constants._module is None:
        print("The type library could not be read, so there is nothing to compare "
              "the table against.\nRepair the pywin32 cache first: delete "
              "%LOCALAPPDATA%\\Temp\\gen_py and re-run.")
        return 1

    if args.find:
        found = sorted(
            (name, value) for name, value in vars(constants._module).items()
            if name.startswith("k") and isinstance(value, int)
            and any(part.lower() in name.lower() for part in args.find)
        )
        print(f"Every enum matching {args.find} in Inventor's own type library:\n")
        for name, value in found:
            known = FALLBACK.get(name)
            note = "" if known is None else (
                "   # the table agrees" if known == value
                else f"   # THE TABLE SAYS {known}")
            print(f'    "{name}": {value},{note}')
        print(f"\n{len(found)} name(s).")
        return 0

    names = sorted(FALLBACK)
    if args.only:
        names = [n for n in names if any(part.lower() in n.lower() for part in args.only)]

    agree, differ, missing = [], [], []
    for name in names:
        actual = getattr(constants._module, name, None)
        if not isinstance(actual, int):
            missing.append(name)
        elif actual == FALLBACK[name]:
            agree.append((name, actual))
        else:
            differ.append((name, FALLBACK[name], actual))

    print(f"Inventor's type library, against the fallback table "
          f"({len(names)} name(s) checked)\n")
    if differ:
        print("WRONG IN THE TABLE -- Inventor says otherwise:")
        for name, table, actual in differ:
            note = f"   ({SUSPECT[name]})" if name in SUSPECT else ""
            print(f'    "{name}": {actual},   # table said {table}{note}')
        print()
    if missing:
        print("NOT IN THIS TYPE LIBRARY -- the name may have changed, or be "
              "unavailable on this version:")
        for name in missing:
            print(f"    {name}   (table says {FALLBACK[name]})")
        print()
    print(f"{len(agree)} entr(ies) already correct, {len(differ)} wrong, "
          f"{len(missing)} not found.")
    if differ:
        print("\nPaste the WRONG block above into FALLBACK in "
              "inventor_mcp/backend/com/constants.py.")
    for name in SUSPECT:
        if name in dict((n, v) for n, v, _ in differ) or name in missing:
            continue
        if any(name == n for n, _ in agree):
            print(f"  note: {name} was disputed and the table turns out to be right; "
                  "it can come off the SUSPECT list.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
