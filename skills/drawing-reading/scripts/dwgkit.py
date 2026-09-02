"""Reading engineering drawings off a raster, and the traps in doing it.

Everything in this session that worked, in one place. Written after building
five parts the slow way, so the next one does not repeat the detours.

THE SCALE IS FREE. Every DAuto sheet here is 1:1 and every one of them comes
out at the same pt/mm: A4 841.68/297, A2 1684.08/594, A1 2384.16/841. So do NOT
fit the scale -- fitting it is how the first pressure-plate read went 25% wrong.
Take it from the sheet size and use a stated dimension as the check.

MEASURE EXTENSION LINES, DO NOT READ ARROWHEADS. At 1:1 on an A-size sheet an
arrowhead is two or three pixels and choosing which station a dimension runs
between is guessing. A dimension's extension lines are long thin runs whose
positions are exact; find them all, then for each stated dimension find the
pair whose spacing equals it. On the spark plug all six landed inside 0.099 mm
and it corrected a chain I had wrong. Detail A's 0.7, 3.6 and 1.2 then fell out
of the same stations without being used -- that is the check worth having.

FOR SHAPES, TRACE THE SILHOUETTE, NOT THE STROKE. Stroke following dies wherever
anything crosses the outline, and on a real drawing everything crosses it:
extension lines, leaders, hatching, and a chain-dash centreline that is thick
enough to pass a width gate but broken. Flood the exterior instead and walk the
region's boundary; a boundary cannot be diverted by a line crossing it.

CROSS-CHECK ON SOMETHING THE CALIBRATION NEVER SAW. The housing flange was
calibrated on the rectangular end's 102 and then measured Ø114.5 where the
drawing said 114. Two independent numbers agreeing is evidence; self-consistency
is not.

TRAPS, all paid for once already:

  * `slot.length` is centre-to-centre of the END ARCS, and each arc adds half
    the width. Taken as an overall length it put the plate's tabs at r=64.25
    instead of 58, and once built a 29 mm slot for a stated 20.
  * a pattern `count` must be unitless; passing a parameter makes it a length.
  * Douglas-Peucker degenerates on a CLOSED path -- first and last points
    coincide, the opening segment has zero length, every distance measures zero
    and the outline collapses to two points. Anchor three.
  * r(theta) by bucketing vertices leaves gaps wherever decimation removed the
    intermediate points of a straight run. Intersect rays with the segments.
  * clear a border ring before flooding: it gives every leader entering from
    outside a FREE END, so the flood rounds its tip instead of being sealed out.
    This alone removed the need for a hand mask on the housing.
  * strip leader spikes by opening the REGION, never the ink. The ink is a thin
    stroke erosion can nick; the region is tens of mm across and cannot break.
    Re-take the largest component afterwards -- opening can sever a fragment,
    and the boundary walk starts at the first set pixel.
  * erasing long thin straight runs works, but a circle's tangent rows are long
    ink runs too. The span threshold has to separate a 116 mm dimension line
    from a ~7 mm tangent run.
  * Inventor: `feature` comes back null on a selector, and `convex`/`concave`
    only classify LINEAR edges, so a circular rim needs position and length.
  * Inventor: a variable-radius fillet needs one edge set PER EDGE and an
    EdgeCollection, not an ObjectCollection.
  * Inventor: shell refused any thickness above 3 while 6 mm-thick bolt lugs
    were present. Lugs are the flange's own thickness -- build them after.
  * Inventor: RangeBox is loose on curved faces. A Ø50 drill reads 51.14.
"""
import math
from collections import deque

import pymupdf

#: pt per mm for a 1:1 sheet, from the page width and the ISO sheet's width.
ISO_WIDTH_MM = {"A0": 1189.0, "A1": 841.0, "A2": 594.0, "A3": 420.0,
                "A4": 297.0}


def sheet_scale(path, sheet=None):
    """pt per mm for a 1:1 drawing, taken from the sheet size.

    With no `sheet`, the ISO size whose width best matches the page is used and
    reported, so a wrong guess is visible rather than silent.
    """
    doc = pymupdf.open(path)
    width = doc[0].rect.width
    height = doc[0].rect.height
    doc.close()
    if sheet:
        return width / ISO_WIDTH_MM[sheet], sheet
    best = min(ISO_WIDTH_MM,
               key=lambda s: abs(width / ISO_WIDTH_MM[s] - 2.8346))
    return width / ISO_WIDTH_MM[best], best


def ink(path, clip, zoom, threshold=175):
    """The crop as a 0/1 ink mask."""
    doc = pymupdf.open(path)
    pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom),
                            clip=pymupdf.Rect(*clip), colorspace="gray")
    w, h, stride, buf = pix.width, pix.height, pix.stride, pix.samples
    mask = bytearray(w * h)
    for y in range(h):
        row, out = y * stride, y * w
        for x in range(w):
            if buf[row + x] < threshold:
                mask[out + x] = 1
    doc.close()
    return mask, w, h


def _runs(counts, need, max_width):
    out, i = [], 0
    n = len(counts)
    while i < n:
        if counts[i] >= need:
            start = i
            while i < n and counts[i] >= need:
                i += 1
            if i - start <= max_width:
                weight = sum(counts[k] for k in range(start, i)) or 1
                out.append(sum(k * counts[k] for k in range(start, i)) / weight)
        else:
            i += 1
    return out


def lines(mask, w, h, across=False, min_span=0.18, max_width=14):
    """Extension-line positions: long thin runs, along or across the view.

    max_width matters: they measure 7 px at zoom 10, and a limit of 6 rejected
    every one of them.
    """
    if across:
        counts = [sum(1 for x in range(w) if mask[y * w + x]) for y in range(h)]
        return _runs(counts, int(min_span * w), max_width)
    counts = [0] * w
    for y in range(h):
        row = y * w
        for x in range(w):
            if mask[row + x]:
                counts[x] += 1
    return _runs(counts, int(min_span * h), max_width)


def stations(path, clip, zoom, pt_per_mm, wanted, across=False, tol=0.15):
    """Where each stated dimension actually runs, at a KNOWN scale.

    Returns (positions_mm, [(value, a_mm, b_mm, error_mm, ok)]).
    """
    mask, w, h = ink(path, clip, zoom)
    pos = lines(mask, w, h, across=across)
    px_per_mm = pt_per_mm * zoom
    origin = min(pos) if pos else 0.0
    mm = [(p - origin) / px_per_mm for p in pos]
    found = []
    for value in wanted:
        best = None
        for i in range(len(mm)):
            for j in range(i + 1, len(mm)):
                err = abs((mm[j] - mm[i]) - value)
                if best is None or err < best[0]:
                    best = (err, mm[i], mm[j])
        if best:
            found.append((value, best[1], best[2], best[0], best[0] <= tol))
    return mm, found


def crossings(mask, w, h, row):
    """Ink crossings along one row, as pixel centres. For concentric circles."""
    out, x = [], 0
    while x < w:
        if mask[row * w + x]:
            s = x
            while x < w and mask[row * w + x]:
                x += 1
            out.append((s + x - 1) / 2.0)
        else:
            x += 1
    return out


# --------------------------------------------------------------------------
# silhouette tracing
# --------------------------------------------------------------------------

def _clear_ring(mask, w, h, ring):
    for y in range(h):
        row = y * w
        edge = y < ring or y >= h - ring
        for x in range(w):
            if edge or x < ring or x >= w - ring:
                mask[row + x] = 0


def _flood(mask, w, h):
    outside = bytearray(w * h)
    queue = deque()
    for x, y in ([(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)]
                 + [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]):
        i = y * w + x
        if not mask[i] and not outside[i]:
            outside[i] = 1
            queue.append(i)
    while queue:
        i = queue.popleft()
        x, y = i % w, i // w
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h:
                j = ny * w + nx
                if not mask[j] and not outside[j]:
                    outside[j] = 1
                    queue.append(j)
    return outside


def _component(pred, w, h, seed=None):
    """Largest region where pred(i) is true, or the one containing seed."""
    if seed is not None:
        i0 = seed[1] * w + seed[0]
        if not pred(i0):
            return None, 0
        starts = [i0]
    else:
        starts = None
    seen = bytearray(w * h)
    best, size = None, 0
    candidates = starts if starts else range(w * h)
    for start in candidates:
        if not pred(start) or seen[start]:
            continue
        queue = deque([start])
        seen[start] = 1
        cells = []
        while queue:
            i = queue.popleft()
            cells.append(i)
            x, y = i % w, i // w
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < w and 0 <= ny < h:
                    j = ny * w + nx
                    if pred(j) and not seen[j]:
                        seen[j] = 1
                        queue.append(j)
        if len(cells) > size:
            size, best = len(cells), cells
    out = bytearray(w * h)
    for i in best or []:
        out[i] = 1
    return out, size


def _morph(mask, w, h, rounds, dilate):
    for _ in range(rounds):
        for horiz in (True, False):
            nxt = bytearray(w * h)
            for y in range(h):
                row = y * w
                up, dn = max(y - 1, 0) * w, min(y + 1, h - 1) * w
                for x in range(w):
                    if horiz:
                        a = mask[row + max(x - 1, 0)]
                        b = mask[row + x]
                        c = mask[row + min(x + 1, w - 1)]
                    else:
                        a, b, c = mask[up + x], mask[row + x], mask[dn + x]
                    nxt[row + x] = 1 if ((a or b or c) if dilate
                                         else (a and b and c)) else 0
            mask = nxt
    return mask


_AROUND = ((-1, 0), (-1, -1), (0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1))


def _walk(mask, w, h):
    start = next((i for i in range(w * h) if mask[i]), None)
    if start is None:
        return []
    start = (start % w, start // w)

    def inside(x, y):
        return 0 <= x < w and 0 <= y < h and mask[y * w + x]

    path, cur, came = [start], start, 0
    for _ in range(8 * w * h):
        for k in range(1, 9):
            d = (came + k) % 8
            nxt = (cur[0] + _AROUND[d][0], cur[1] + _AROUND[d][1])
            if inside(*nxt):
                came, cur = (d + 5) % 8, nxt
                path.append(cur)
                break
        else:
            break
        if cur == start and len(path) > 8:
            return path
    return path


def decimate(points, tol):
    """Douglas-Peucker on a CLOSED path: three anchors, not two."""
    if len(points) < 6:
        return list(points)
    n = len(points)
    keep = [False] * n
    anchors = [0, n // 3, 2 * n // 3, n - 1]
    for a in anchors:
        keep[a] = True
    stack = [(anchors[i], anchors[i + 1]) for i in range(len(anchors) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = points[i]
        dx, dy = points[j][0] - ax, points[j][1] - ay
        norm = math.hypot(dx, dy) or 1.0
        worst, at = -1.0, None
        for k in range(i + 1, j):
            d = abs(dx * (ay - points[k][1]) - dy * (ax - points[k][0])) / norm
            if d > worst:
                worst, at = d, k
        if worst > tol:
            keep[at] = True
            stack += [(i, at), (at, j)]
    return [p for p, k in zip(points, keep) if k]


def silhouette(path, clip, zoom, pt_per_mm, ring=20, tol=0.10, seed=None):
    """The part's outline in mm, about its own bounding-box centre."""
    mask, w, h = ink(path, clip, zoom)
    _clear_ring(mask, w, h, ring)
    outside = _flood(mask, w, h)
    region, size = _component(lambda i: not outside[i], w, h, seed)
    if region is None:
        return None, {}
    region = _morph(region, w, h, max(2, int(0.9 * zoom / 2)), dilate=False)
    region = _morph(region, w, h, max(2, int(0.9 * zoom / 2)), dilate=True)
    region, kept = _component(lambda i: region[i], w, h)
    boundary = _walk(region, w, h)
    stats = {"outside_pct": 100.0 * sum(outside) / (w * h),
             "region_pct": 100.0 * size / (w * h),
             "kept_pct": 100.0 * kept / (w * h),
             "closed": len(boundary) > 8 and boundary[0] == boundary[-1],
             "points": len(boundary)}
    if not stats["closed"]:
        return None, stats
    px_per_mm = pt_per_mm * zoom
    xs = [p[0] for p in boundary]
    ys = [p[1] for p in boundary]
    cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    mm = [((x - cx) / px_per_mm, -(y - cy) / px_per_mm) for x, y in boundary]
    thin = decimate(mm, tol)
    stats["thinned"] = len(thin)
    stats["extent"] = (max(p[0] for p in thin) - min(p[0] for p in thin),
                       max(p[1] for p in thin) - min(p[1] for p in thin))
    return thin, stats


def r_theta(poly, step=1.0):
    """Radius against angle, by intersecting rays with the polygon's segments."""
    n = len(poly)
    a2 = cx = cy = 0.0
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        a2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    centre = ((cx / (3.0 * a2), cy / (3.0 * a2)) if abs(a2) > 1e-9
              else (sum(p[0] for p in poly) / n, sum(p[1] for p in poly) / n))
    out = []
    a = 0.0
    while a < 360.0:
        dx, dy = math.cos(math.radians(a)), math.sin(math.radians(a))
        best = None
        for i in range(n):
            x0, y0 = poly[i][0] - centre[0], poly[i][1] - centre[1]
            x1, y1 = poly[(i + 1) % n][0] - centre[0], poly[(i + 1) % n][1] - centre[1]
            ex, ey = x1 - x0, y1 - y0
            den = dx * ey - dy * ex
            if abs(den) < 1e-12:
                continue
            t = (x0 * ey - y0 * ex) / den
            u = (x0 * dy - y0 * dx) / den
            if t >= 0.0 and -1e-9 <= u <= 1.0 + 1e-9 and (best is None or t > best):
                best = t
        if best is not None:
            out.append((a, best))
        a += step
    return centre, out
