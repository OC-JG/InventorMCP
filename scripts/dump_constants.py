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


def every_name(module) -> dict[str, int]:
    """Every integer constant in the type library, by name.

    Not ``vars(module)``. ``win32com.client.constants`` is not a module: it is
    an object holding a *list* of dictionaries, one per generated type-library
    module, and its ``__getattr__`` walks that list. So ``getattr`` finds
    everything and ``vars`` finds nothing -- its ``__dict__`` contains the single
    key ``__dicts__``.

    That is why ``--find Shell`` printed "0 name(s)" on a machine that had just
    read ``kInsideShellDirection`` from the same object two commands earlier. An
    empty search result read as "this release does not have it", which is a much
    stronger claim than "I looked in the wrong place".
    """
    found: dict[str, int] = {}
    for source in getattr(module, "__dicts__", None) or [vars(module)]:
        for name, value in source.items():
            if isinstance(value, int) and not name.startswith("_"):
                found.setdefault(name, value)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", default=[],
                        help="Substrings to filter names by, e.g. Dim Surface.")
    parser.add_argument("--value", nargs="*", default=[], type=int, metavar="N",
                        help="Name every enum with one of these values. Use it when "
                             "Inventor hands back a number nothing recognises -- a "
                             "feature reporting HealthStatus 11778 is only a mystery "
                             "until you ask what 11778 is called.")
    parser.add_argument("--find", nargs="*", default=[], metavar="SUBSTRING",
                        help="List every enum in the type library whose name "
                             "contains one of these, whether the table knows it or "
                             "not. Use it when a value is needed and there is "
                             "nothing to compare against -- 'Health' settles what "
                             "a feature's HealthStatus means.")
    args = parser.parse_args(argv)

    # Connect first. `load()` on its own found nothing here, and the message it
    # printed -- "repair the pywin32 cache" -- sent the reader after a cache that
    # was perfectly healthy: the acceptance run read fifty-one values from it
    # minutes earlier. What it actually needed was a live application to ask.
    constants = load()
    if constants._module is None:
        from inventor_mcp.session import Session

        try:
            backend = Session(backend_kind="inventor").ensure_backend()
            backend.connect(visible=True, create=True)
        except Exception as exc:
            print(f"Could not reach Inventor: {exc}")
            return 1
        constants = getattr(backend, "unmarshalled", backend)._constants
    if constants._module is None:
        print("The type library could not be read, so there is nothing to compare "
              "the table against.\nRepair the pywin32 cache first: delete "
              "%LOCALAPPDATA%\\Temp\\gen_py and re-run.")
        return 1

    if args.value:
        wanted = set(args.value)
        found = sorted(
            (value, name) for name, value in every_name(constants._module).items()
            if value in wanted
        )
        for number in args.value:
            names = [name for value, name in found if value == number]
            print(f"{number}: " + (", ".join(names) if names else "no enum has this value"))
        return 0

    if args.find:
        # Not restricted to names beginning with k: the first version was, and
        # `--find Health` reported "0 names" on a library that certainly has a
        # HealthStatusEnum. A search that can only find what it expects is not a
        # search.
        every = every_name(constants._module)
        found = sorted(
            (name, value) for name, value in every.items()
            if any(part.lower() in name.lower() for part in args.find)
        )
        print(f"Every enum matching {args.find} in Inventor's own type library:\n")
        for name, value in found:
            known = FALLBACK.get(name)
            note = "" if known is None else (
                "   # the table agrees" if known == value
                else f"   # THE TABLE SAYS {known}")
            print(f'    "{name}": {value},{note}')
        # How many were searched, not only how many matched. "0 name(s)" alone
        # cannot distinguish "this release has no such enum" from "nothing was
        # searched", and those call for opposite conclusions.
        print(f"\n{len(found)} of {len(every)} name(s) in the library matched.")
        if not found:
            if not every:
                print("Nothing was searched: the type library read as empty, which is a "
                      "fault here rather than a fact about Inventor.\n"
                      "Repair the pywin32 cache: delete %LOCALAPPDATA%\\Temp\\gen_py "
                      "and re-run.")
            else:
                print(f"{len(every)} names were searched and none contains "
                      f"{' or '.join(args.find)}. That is an answer: this release has no "
                      "such enum under a name spelled that way.")
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
