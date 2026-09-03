---
name: drawing-reading
description: Model a part from an engineering drawing that is a scan, photo or image — a JPEG/PNG drawing, or a PDF whose drawing is an embedded raster rather than vector. Use when the user points at a drawing and wants the part built, when dimensions have to be got off a raster accurately, or when a first attempt "looks nothing like the drawing". Covers getting the scale for free from the sheet size, measuring extension lines instead of reading arrowheads, tracing an outline by silhouette, and reading a revolved profile off an axial section. Triggers on: drawing, DWG, DXF, scanned drawing, "build this from the drawing", "model this part", views/sections (front view, section A-A, detail A), or a dimension that cannot be pinned by eye.
---

# Reading an engineering drawing off a raster

For a drawing that is a scan or an image. If you have a **vector** PDF, DXF or
DWG, stop and read that instead — `scripts/dxf.py` parses a DXF's entities
exactly and everything below becomes unnecessary. See "Get the vector first".

Written after building five parts from raster drawings and getting three of them
visibly wrong first. Each rule below is a mistake already paid for.

## Get the vector first

Ask before you measure. These drawings are CAD output; a raster is a lossy
print of geometry that exists exactly somewhere.

- A PDF is not automatically vector. Check: `page.get_drawings()` returning a
  few dozen paths that are all frame and title block, plus `page.get_images()`,
  means the drawing is an embedded raster. One RS datasheet held its drawing as
  a 641×636 image at 3.4 px/pt — about 160 px per view, **coarser than the
  JPEGs**, so measuring tools cannot work on it.
- A DWG must be converted. Inventor will open one and even export a DXF, but
  only of its own sheet frame — it treats the AutoCAD model space as
  non-editable, so the geometry never reaches the export. `accoreconsole.exe`
  hangs on a licence check. The quick route is to ask the user to Save As DXF
  from AutoCAD; it takes them seconds.

## The scale is free — never fit it

Every sheet at 1:1 gives the same pt/mm, and the page size tells you which:
A4 841.68/297, A2 1684.08/594, A1 2384.16/841 all come to **2.835 pt/mm**.

Fitting the scale instead is how a pressure-plate read went 25% wrong: the
"102 across" it calibrated on was really the 122 lug-to-lug span, because the
bolt lugs stand proud of the flange edge. Take the scale from the sheet and use
a stated dimension as the check.

## Measure extension lines; do not read arrowheads

At 1:1 an arrowhead is two or three pixels. Choosing which station a dimension
runs between is guessing, and it is the single biggest source of a wrong part.

A dimension's extension lines are long thin runs whose positions are exact.
Find them all, then for each dimension the drawing states, find the pair whose
spacing equals it:

```python
from dwgkit import sheet_scale, stations
pt_per_mm, sheet = sheet_scale(path)            # 2.8352, 'A2'
mm, hits = stations(path, clip=(35, 25, 250, 155), zoom=10,
                    pt_per_mm=pt_per_mm,
                    wanted=[35.2, 17.4, 15, 5, 4.9, 3.1])
```

On a spark plug all six landed inside **0.099 mm**, and it corrected an axial
chain that had been read by eye — the 5 was the flange at 22.95→28.05, not
where the eye put it, and the 15 was the body rather than unplaced.

Then check on dimensions you did **not** use. Detail A's 0.7, 3.6 and 1.2 all
fell out of those same stations to within 0.1 mm. Agreement with numbers the fit
never saw is evidence; self-consistency is not.

`max_width` matters: extension lines measure 7 px at zoom 10, and a limit of 6
rejects every one of them silently.

## For a shape, trace the silhouette — not the stroke

Stroke following dies wherever anything crosses the outline, and on a real
drawing everything crosses it: extension lines, leaders, hatching, and a
chain-dash centreline thick enough to pass a width gate but broken so the trace
dies in a gap. Every fix bought a few hundred more steps and failed elsewhere.

Flood the exterior and walk the region's boundary instead. A region boundary
cannot be diverted by a line crossing it, because the line is inside the region.

```python
from dwgkit import silhouette, r_theta
poly, stats = silhouette(path, clip, zoom=8, pt_per_mm=pt_per_mm, ring=22)
centre, profile = r_theta(poly)                  # radius against angle
```

- **Clear a border ring first.** That gives every leader entering from outside a
  free end, so the flood rounds its tip instead of being sealed out by it. This
  alone removed the need for any hand mask.
- **Strip leader spikes by opening the REGION, never the ink.** The ink is a
  thin stroke erosion can nick, and one nick lets the flood in and the part
  falls apart; the region is tens of mm across and cannot break. Re-take the
  largest component afterwards, because opening can sever a fragment and the
  boundary walk starts at the first set pixel.
- Sanity check with `stats`: outside% + region% should account for nearly the
  whole raster. Do not gate on region% alone — a lobed disc genuinely fills two
  thirds of a tight crop, and treating that as a leak rejects a correct answer.
- Erasing long thin straight runs does remove dimension lines, but **a circle's
  tangent rows are long ink runs too**; the span threshold has to separate a
  116 mm dimension line from a ~7 mm tangent run. Get this wrong and it erases
  the part.

## Read a revolved profile off its section

For anything turned, the axial section gives the whole profile. Scan it column
by column: cropped to the part's band, the topmost and bottommost ink in a
column ARE its outline. Skip the columns where an extension line crosses — you
measured those exactly already.

That is how a spark plug's electrode end was read (the taper leaves the thread
at 52.63 against a 52.517 measured from the front view) and how a thread's
minor diameter came out at Ø9.1, consistent with a Ø11.4 major at 2 mm pitch.
The stated diameters then become a check on the profile rather than its source.

## Traps that cost a rebuild each

- **Douglas-Peucker degenerates on a closed path.** First and last points
  coincide, the opening segment has zero length, every perpendicular distance
  measures zero, and the outline collapses to two points. Anchor three.
- **r(θ) by bucketing vertices leaves gaps** wherever decimation removed the
  intermediate points of a straight run — which looks exactly like missing
  sectors in the trace. Intersect rays with the segments.
- **A leader's own text is as thick as the outline.** No width rule and no
  morphological reconstruction removes it; 97% of the ink came back. Either
  clear the border ring so it cannot seal a pocket, or name the region.
- **A drawing can contradict itself.** The drill's tip is over-specified — 60
  long, 20°, 120° and a Ø14 flat cannot all hold. Taking the angles gave 377.97
  overall against the drawing's own 200 + 180. Prefer the lengths when they are
  stated twice and agree, and say what the discrepancy is: the tip came out
  117.9° against a stated 120.
- **Decide the point count against the drawing's own line width.** A traced
  outline at 0.25 mm is 295 sketch points and minutes of constraint solving; at
  0.5 mm it is 90 points and finer than the line you traced.

## Say what you interpreted

Every part built this way has a header listing what was measured, with the
error, and what was interpreted, with why. When a reading is a guess, the model
is only as good as the guess and the next person needs to know which numbers
those are. "Recorded but unused" is a legitimate outcome for a dimension you
could not pin — better than inventing a station for it.
