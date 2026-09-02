---
name: datasheet-reading
description: Get real dimensions out of a component datasheet or mechanical drawing PDF, so a part can be modelled from it. Use whenever the user points at a datasheet, drawing, or spec PDF and wants the numbers — package outline, body size, land pattern, pitch, pin count, ratings — or asks to model a connector, chip, or bought-in part "from this datasheet". Also use when a PDF will not read, comes back blank, has no text, is hundreds of pages, or when dozens of datasheets have to be sorted through at once. Triggers on .pdf plus any of: datasheet, drawing, outline, package dimensions, land pattern, footprint, LCSC/JLC part code, DIMA/DIMB tables.
---

# Reading a datasheet well enough to model from it

Measured against a 232-file, 370 MB library of LCSC datasheets and a Kinghelm
connector drawing. The numbers quoted here are what those files actually did.

## The one-line version

Locate the page textually, then extract rows from **that page only**, and render
a page image only when the text will not parse. Never read a datasheet linearly.

## Before anything: five facts about datasheet libraries

1. **Copy to local disk first.** The same survey over a Google Shared Drive
   (`G:\...`) timed out at 120 s; from local disk it finished in **3 s**. Network
   round-trips per page dominate everything else.

2. **A third of the files are duplicates.** 232 files fingerprinted to **153
   distinct documents**. One 9-page thick-film resistor sheet was shared by **52
   different part numbers**; one 84-page MLCC sheet by 16; one 296-page Murata
   catalogue by 4. Fingerprint before reading, or the same document is read fifty
   times.

3. **The filename is not a key you can search for.** These are LCSC codes
   (`C25091.pdf`), and the code appeared inside its own PDF in **0 of 14** files
   sampled. You cannot find the part within a family sheet from the filename.
   Get the manufacturer part number, or at minimum the case size, from the BOM,
   the library, or the user — then search for *that*.

4. **Almost everything has a text layer.** Of 232 files: 163 rich, 53 moderate,
   8 sparse, **8 image-only**. So text extraction is the default and rendering is
   the exception — the reverse of the instinct a drawing gives you.

5. **Most datasheets are family sheets, not part sheets.** The part-specific
   numbers sit in a table keyed by case size (`0603`) or by dimension letter
   (`D`, `E`, `HE`). Reading the prose gets you the scope statement and the
   storage conditions; the row is what you came for.

## The ladder

Stop at the first rung that gives you the numbers.

### 1. Fingerprint and dedupe
Hash the first 64 KB plus the file size. Cheap, and enough to collapse a library.

### 2. Read the text layer
`page.get_text()`. If the first few pages give zero characters, it is one of the
image-only files — skip to rung 5.

### 3. Locate the section by heading
Match short lines against a heading vocabulary:

    package dimensions | dimensions | outline | mechanical | physical
    absolute maximum | ratings | recommended land | land pattern | footprint
    marking | ordering | soldering | taping

This is what turns an 84-page sheet into "pages 6, 30, 35, 36, 47, 50".

### 4. Extract rows from the located pages only
Rebuild the **visual rows** by clustering words on their y coordinate
(`page.get_text("words")`, bucket `y0` to ~3 pt). Then take rows with three or
more decimal numbers.

Why not `find_tables()`: it needs ruling lines. On the resistor sheet's ratings
table it found **0 tables**; `strategy="text"` found it but split the header
across 73 rows x 13 columns. Word clustering handles ruled and borderless alike.

Why "located pages only": the same relaxed row rule over a whole document
returned **257 candidates** on the 84-page MLCC — graph axis ticks, marking
codes, packaging codes. Restricted to the located pages it gave **103**, and the
first ones were the case-size table. On a 3-page diode sheet it went from 12
noisy rows to 10 clean ones.

Look up by key once you have rows. Searching the resistor sheet for `0603`
returns the whole part spec in one pass:

    p4 | 0603 1.60±0.10 0.80±0.10 0.45±0.10 0.30±0.20 0.30±0.20   <- body
    p4 | 0603 1/10W 1R-10MR ...                                    <- power
    p5 | 0603 75V 150V 300V <50mR 1A 2A -55C~155C                  <- ratings
    p5 | 0603 0.9±0.05 0.65±0.05 0.8±0.05 2.1±0.05                 <- land pattern

Semiconductor sheets key on the dimension letter instead, and give both units:

    p3 | A  0.89 1.00 1.11   0.035 0.040 0.044
    p3 | D  2.80 2.90 3.04   0.110 0.114 0.120     <- SOT-23, mm then inches

### 5. Render only the page you located
For image-only files, and for CAD-style drawings whose dimensions are vector
graphics rather than text, render at 3-4x and read it:

    page.get_pixmap(matrix=pymupdf.Matrix(3, 3)).save("p.png")

A full page at 3x is about 2500x1750 — readable for a datasheet, **not** readable
for a dense A4 mechanical drawing. For those, crop first: pass `clip=` a
`pymupdf.Rect` around the view you want and render that at 8-9x. On the Kinghelm
connector, the full page was illegible and a cropped elevation at 8x was clear.

### 6. Cross-check before you model
Every number that reaches a model gets one sanity check:

* Against a standard: an 0603 chip **must** be 1.60 x 0.80. If the row says
  otherwise, the wrong row was read.
* Against the drawing's own table: derive the relation and test it on other rows.
  The Kinghelm table gave `DIMA = (ckt-1) x pitch`, `DIMB = DIMA + 2.10`,
  `DIMC = DIMA + 5.90` — checked against all 27 rows before being trusted, which
  is what made the model parametric over the whole 4-30 pin family instead of
  hard-coded for one.
* Against arithmetic: build the part, then check the measured volume against a
  hand calculation. That is what catches a misread dimension.

## Recording what you could not read

A datasheet gives an outline and a land pattern. It does not give internal
geometry. When modelling from one, mark every derived number in the recipe
comments — `"comment": "DERIVED: tail thickness is not dimensioned"` — and say
plainly which features were left out. A fit-check envelope presented as a
manufacturing model is the failure mode here.

## Gotchas that cost time

* **`get_drawings()` and `get_images()` are O(vector content)** and will hang a
  corpus survey. Never call them in a sweep; page count and text length are
  enough to classify.
* **Set `PYTHONIOENCODING=utf-8`.** Chinese datasheets are common and printing a
  snippet crashes cp1252 consoles with `UnicodeEncodeError`.
* **`pdftoppm` is often not installed**, so a host's built-in PDF page rendering
  may be unavailable and return nothing for a scanned sheet. PyMuPDF renders
  without it. Install into a scratch directory (`pip install --target ...`)
  rather than into a project venv.
* **Drive links are not files.** A private Google Drive URL fetched with `curl`
  returns a sign-in HTML page, not the PDF; check the bytes before parsing them.

`scripts/datasheet.py` implements rungs 1-5.
