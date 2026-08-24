"""Naming the next version of a part file, and copying to it.

The DFM loop changes a part. Doing that to the file somebody handed over is
wrong twice: their work is gone, and there is nothing left to compare the result
against. So the loop works on a copy, and the copy is named the way a person
names one, so the sequence reads as a sequence.

Two rules, and between them they cover what actually happens on a shared drive:

* a name that already carries a version gets the next one -- ``bracket_v2.ipt``
  becomes ``bracket_v3.ipt``, keeping whatever separator and zero-padding it
  used, because ``bracket_v002`` next to ``bracket_v3`` sorts wrong in every
  file browser there is;
* a name that carries none gets ``_v2``, on the reading that the file it is a
  copy of was version 1.

Nothing here ever overwrites: if the next name is taken, it keeps counting. That
matters more than it looks. Two loop runs an hour apart would otherwise land on
the same name, and the second would silently destroy the first -- including the
copy somebody had already reviewed.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

#: A trailing version, as people actually write them: ``_v2``, ``-V03``, ``.v10``.
#: The separator and the padding are captured so the next name matches the last.
_VERSION = re.compile(r"^(?P<stem>.*?)(?P<separator>[_\-. ])(?P<v>[vV])(?P<number>\d+)$")

#: Where the declaration for a part lives when it cannot live in the part. Copied
#: alongside, or a versioned copy would arrive having forgotten which parameter
#: was the wall and which dimensions were not to be touched.
SIDECAR_SUFFIX = ".dfm.json"

#: How far to count before giving up looking for a free name. A directory with
#: this many versions of one part has a different problem.
_LIMIT = 999


def split_version(stem: str) -> tuple[str, str, int, int]:
    """Break *stem* into (base, separator-and-v, number, digits).

    ``digits`` is the zero-padding the name used and the separator carries the
    case of the ``v``, so the next name matches the last one in every respect a
    person would notice. A stem with no version comes back as version one, which
    is what an unversioned file is.
    """
    match = _VERSION.match(stem)
    if match is None:
        return stem, "_v", 1, 1
    number = match.group("number")
    return (match.group("stem"), match.group("separator") + match.group("v"),
            int(number), len(number))


def format_version(base: str, separator: str, number: int, digits: int) -> str:
    return f"{base}{separator}{number:0{digits}d}"


def next_version(path: str | Path, *, taken: set[Path] | None = None) -> Path:
    """The next free version of *path*, without touching anything.

    *taken* is for reserving names that are about to exist but do not yet --
    without it, planning several copies in one breath plans them all onto the
    same name.
    """
    source = Path(path)
    base, separator, number, digits = split_version(source.stem)
    reserved = taken or set()
    for step in range(1, _LIMIT + 1):
        candidate = source.with_name(
            format_version(base, separator, number + step, digits) + source.suffix
        )
        if not candidate.exists() and candidate not in reserved:
            return candidate
    raise FileExistsError(
        f"Every name from {format_version(base, separator, number + 1, digits)} "
        f"to {format_version(base, separator, number + _LIMIT, digits)} is taken."
    )


def working_copy(path: str | Path, *, destination: str | Path | None = None) -> Path:
    """Copy *path* to its next version and return where it went.

    The original is read and never written, which is the point: a filesystem copy
    cannot modify what it copies, where opening the file in Inventor and saving
    it elsewhere leaves a window in which it could. A part's sidecar declaration
    travels with it, so the copy knows which parameter is the wall and what it
    may not touch.
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"There is no file at {source}.")
    target = Path(destination) if destination else next_version(source)
    if target.exists():
        raise FileExistsError(
            f"{target} already exists, and overwriting a version is how the one "
            f"somebody already reviewed disappears."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)

    sidecar = sidecar_for(source)
    if sidecar.is_file():
        shutil.copy2(sidecar, sidecar_for(target))
    return target


def sidecar_for(path: str | Path) -> Path:
    """Where *path*'s declaration lives beside it."""
    source = Path(path)
    return source.with_name(source.stem + SIDECAR_SUFFIX)


def versions_of(path: str | Path) -> list[Path]:
    """Every version of *path* that exists, in order.

    Ordered by version number rather than by name, so ``v9`` comes before
    ``v10`` -- which sorting the strings would get backwards.
    """
    source = Path(path)
    base, _, _, _ = split_version(source.stem)
    # Any separator and either case, because the sequence on a shared drive is
    # rarely as tidy as the one that started it.
    pattern = re.compile(rf"^{re.escape(base)}[_\-. ][vV](\d+)$")
    found: list[tuple[int, Path]] = []
    for candidate in source.parent.iterdir():
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() != source.suffix.lower():
            continue
        if candidate.stem == base:
            found.append((1, candidate))
            continue
        match = pattern.match(candidate.stem)
        if match:
            found.append((int(match.group(1)), candidate))
    return [candidate for _, candidate in sorted(found, key=lambda pair: pair[0])]
