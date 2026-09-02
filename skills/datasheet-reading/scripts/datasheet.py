"""Get dimensions out of a datasheet PDF without reading it linearly.

Implements the ladder in SKILL.md. Needs PyMuPDF only:

    pip install --target <scratch> pymupdf     # keep it out of a project venv

Usage
-----
    python datasheet.py survey   <dir>             # fingerprint, dedupe, classify
    python datasheet.py sections <file.pdf>        # where the dimensions live
    python datasheet.py dims     <file.pdf>        # dimension rows, located pages only
    python datasheet.py find     <file.pdf> 0603   # every visual row containing a key
    python datasheet.py render   <file.pdf> 3      # render page 3 to PNG
    python datasheet.py crop     <file.pdf> 1 x0 y0 x1 y1 [zoom]

Every subcommand prints page numbers, because the next step is nearly always
"render that page".
"""

from __future__ import annotations

import collections
import glob
import hashlib
import os
import re
import sys

import pymupdf

# --- what a heading and a dimension row look like ---------------------------

HEADING = re.compile(
    r"(package\s+dimensions|dimensions?|outline|mechanical|physical|"
    r"absolute\s+maximum|ratings?|recommended\s+land|land\s+pattern|footprint|"
    r"marking|ordering|soldering|taping|packaging)", re.I)

DECIMAL = re.compile(r"\d+\.\d+")

#: A visual row needs this many decimals to be worth showing. Three is what
#: separates a dimension row from a sentence with a version number in it.
MIN_DECIMALS = 3


# --- rows ------------------------------------------------------------------

def rows_of(page, tol: float = 3.0):
    """The page's visual rows, top to bottom, as (y, [(x, word), ...]).

    Clustering words by y coordinate reconstructs rows whether or not the table
    is ruled. `find_tables()` needs ruling lines and returns nothing for the
    borderless tables datasheets are full of; `strategy="text"` finds them but
    splits headers across dozens of cells.
    """
    buckets: dict[int, list[tuple[float, str]]] = {}
    for x0, y0, _x1, _y1, word, *_ in page.get_text("words"):
        buckets.setdefault(round(y0 / tol), []).append((x0, word))
    return [(key * tol, sorted(buckets[key])) for key in sorted(buckets)]


def row_text(row) -> str:
    return " ".join(word for _, word in row[1])


def flat_rows(page):
    return [" ".join(row_text(r).split()) for r in rows_of(page)]


# --- the ladder ------------------------------------------------------------

def fingerprint(path: str) -> str:
    """Cheap identity: first 64 KB plus size. Enough to collapse a library."""
    with open(path, "rb") as fh:
        head = fh.read(65536)
    return "%s-%d" % (hashlib.sha256(head).hexdigest()[:12], os.path.getsize(path))


def survey(directory: str) -> None:
    files = sorted(glob.glob(os.path.join(directory, "*.pdf")))
    seen: dict[str, list[str]] = collections.defaultdict(list)
    image_only = []
    for path in files:
        fp = fingerprint(path)
        seen[fp].append(os.path.basename(path))
        if len(seen[fp]) > 1:
            continue                       # only classify one of each document
        try:
            doc = pymupdf.open(path)
            head = "".join(doc[n].get_text() for n in range(min(3, doc.page_count)))
            if not head.strip():
                image_only.append(os.path.basename(path))
            doc.close()
        except Exception as exc:
            print("  cannot open %s: %s" % (os.path.basename(path), exc))
    print("%d files -> %d distinct documents" % (len(files), len(seen)))
    shared = {fp: names for fp, names in seen.items() if len(names) > 1}
    print("%d documents are shared by more than one part number" % len(shared))
    for fp, names in sorted(shared.items(), key=lambda kv: -len(kv[1]))[:5]:
        print("   %3d parts share one document: %s%s"
              % (len(names), ", ".join(names[:6]), " ..." if len(names) > 6 else ""))
    if image_only:
        print("image-only, must be rendered (%d): %s"
              % (len(image_only), ", ".join(image_only[:10])))


def section_pages(doc, limit: int = 200) -> dict[int, list[str]]:
    """Pages whose short lines look like a section heading -> those headings."""
    found: dict[int, list[str]] = {}
    for n in range(min(doc.page_count, limit)):
        for text in flat_rows(doc[n]):
            if text and len(text) < 70 and HEADING.search(text):
                found.setdefault(n, []).append(text[:60])
    return found


def sections(path: str) -> None:
    doc = pymupdf.open(path)
    found = section_pages(doc)
    print("%s: %d pages" % (os.path.basename(path), doc.page_count))
    if not found:
        print("  no headings matched -- try `dims` over the whole file, or `render`")
    for n in sorted(found):
        print("  p%-4d %s" % (n + 1, "; ".join(found[n][:3])))
    doc.close()


def dims(path: str, everywhere: bool = False) -> None:
    """Dimension rows, from the located pages only unless told otherwise.

    The restriction is what makes this usable: the same row rule over a whole
    84-page capacitor sheet returns 257 candidates, mostly graph axis ticks.
    """
    doc = pymupdf.open(path)
    pages = range(doc.page_count) if everywhere else sorted(section_pages(doc))
    if not pages:
        print("no dimension sections found; retrying over the whole document")
        pages = range(doc.page_count)
    hits = 0
    for n in pages:
        for text in flat_rows(doc[n]):
            if len(DECIMAL.findall(text)) >= MIN_DECIMALS and len(text) < 120:
                print("  p%-4d %s" % (n + 1, text[:100]))
                hits += 1
    if not hits:
        print("nothing parsed -- this is a drawing, not a table. Render the page:")
        print("   python datasheet.py render %s <page>" % os.path.basename(path))
    doc.close()


def find(path: str, needle: str) -> None:
    """Every visual row containing `needle` as a whole word, across the file.

    This is the family-sheet lookup: give it a case size and it returns the
    body, the ratings and the land pattern in one pass.
    """
    doc = pymupdf.open(path)
    for n in range(doc.page_count):
        for row in rows_of(doc[n]):
            if needle in [w for _, w in row[1]]:
                print("  p%-4d %s" % (n + 1, " ".join(row_text(row).split())[:110]))
    doc.close()


def render(path: str, page: int, zoom: float = 3.0) -> None:
    doc = pymupdf.open(path)
    pix = doc[page - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    out = "%s_p%d.png" % (os.path.splitext(os.path.basename(path))[0], page)
    pix.save(out)
    print("%s  %dx%d" % (out, pix.width, pix.height))
    if max(pix.width, pix.height) < 2000:
        print("  small -- raise the zoom if the dimensions are not legible")
    doc.close()


def crop(path: str, page: int, box, zoom: float = 8.0) -> None:
    """Render one region. A dense A4 drawing is illegible whole; crop and zoom."""
    doc = pymupdf.open(path)
    pix = doc[page - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom),
                                   clip=pymupdf.Rect(*box))
    out = "%s_p%d_crop.png" % (os.path.splitext(os.path.basename(path))[0], page)
    pix.save(out)
    print("%s  %dx%d" % (out, pix.width, pix.height))
    doc.close()


def main(argv) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd, target = argv[1], argv[2]
    if cmd == "survey":
        survey(target)
    elif cmd == "sections":
        sections(target)
    elif cmd == "dims":
        dims(target, everywhere="--all" in argv)
    elif cmd == "find":
        find(target, argv[3])
    elif cmd == "render":
        render(target, int(argv[3]), float(argv[4]) if len(argv) > 4 else 3.0)
    elif cmd == "crop":
        box = [float(v) for v in argv[4:8]]
        crop(target, int(argv[3]), box, float(argv[8]) if len(argv) > 8 else 8.0)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
