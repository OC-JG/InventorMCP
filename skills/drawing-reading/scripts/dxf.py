"""Read a DXF's entities exactly. No tracing, no thresholds, no calibration.

This is what I asked for and could not get from the rasters: real line
endpoints, arc centres and radii, in drawing units, plus the dimension text as
text. Everything the pixel pipeline had to infer is simply stated here.

DXF is group-code/value pairs, one per line, so a parser is short and there is
no library to install.
"""
import math
import sys
from collections import Counter, defaultdict

#: group codes worth keeping, per entity type
POINTS = {10: "x", 20: "y", 30: "z", 11: "x2", 21: "y2", 31: "z2",
          12: "x3", 22: "y3", 13: "x4", 23: "y4"}
SCALARS = {40: "r", 41: "r2", 42: "bulge", 50: "a0", 51: "a1", 70: "flags",
           90: "count", 62: "colour", 1: "text", 3: "text3", 8: "layer",
           2: "name", 5: "handle", 67: "space", 210: "nx", 220: "ny",
           230: "nz"}


def read(path):
    """Every entity in the file's ENTITIES sections, as dicts."""
    with open(path, "r", errors="replace") as fh:
        lines = [ln.rstrip("\n").rstrip("\r") for ln in fh]
    pairs = []
    for i in range(0, len(lines) - 1, 2):
        try:
            pairs.append((int(lines[i].strip()), lines[i + 1]))
        except ValueError:
            continue

    entities, current, section = [], None, None
    for code, value in pairs:
        if code == 0:
            token = value.strip()
            if token == "SECTION":
                section = "?"
                continue
            if token == "ENDSEC":
                section = None
            if current:
                entities.append(current)
                current = None
            if token not in ("SECTION", "ENDSEC", "EOF", "TABLE", "ENDTAB",
                             "BLOCK", "ENDBLK", "CLASS"):
                current = {"type": token, "verts": []}
            continue
        if code == 2 and section == "?":
            section = value.strip()
            continue
        if current is None:
            continue
        if code == 10 and current["type"] == "LWPOLYLINE":
            current["verts"].append([float(value), None])
            continue
        if code == 20 and current["type"] == "LWPOLYLINE" and current["verts"]:
            current["verts"][-1][1] = float(value)
            continue
        if code in POINTS:
            current[POINTS[code]] = float(value)
        elif code in SCALARS:
            key = SCALARS[code]
            try:
                current[key] = float(value) if code not in (1, 3, 8, 2, 5) \
                    else value
            except ValueError:
                current[key] = value
    if current:
        entities.append(current)
    return entities


def summarise(entities):
    kinds = Counter(e["type"] for e in entities)
    layers = Counter(str(e.get("layer", "")) for e in entities)
    print("%d entities" % len(entities))
    print("by type:  %s" % ", ".join("%s=%d" % kv for kv in kinds.most_common()))
    print("by layer: %s" % ", ".join("%s=%d" % kv
                                     for kv in layers.most_common(12)))

    xs, ys = [], []
    for e in entities:
        for kx, ky in (("x", "y"), ("x2", "y2"), ("x3", "y3")):
            if kx in e and ky in e:
                xs.append(e[kx]); ys.append(e[ky])
        for vx, vy in e.get("verts", []):
            if vy is not None:
                xs.append(vx); ys.append(vy)
    if xs:
        print("extents: X %.3f .. %.3f   Y %.3f .. %.3f   (%.3f x %.3f)"
              % (min(xs), max(xs), min(ys), max(ys),
                 max(xs) - min(xs), max(ys) - min(ys)))
    return kinds


def geometry(entities, layer=None):
    """Just the shape-bearing entities."""
    keep = {"LINE", "CIRCLE", "ARC", "LWPOLYLINE", "POLYLINE", "SPLINE",
            "ELLIPSE"}
    out = [e for e in entities if e["type"] in keep]
    if layer is not None:
        out = [e for e in out if str(e.get("layer", "")) == layer]
    return out


def texts(entities):
    out = []
    for e in entities:
        if e["type"] in ("TEXT", "MTEXT", "ATTRIB"):
            t = str(e.get("text", "") or e.get("text3", "")).strip()
            if t:
                out.append((e.get("x", 0.0), e.get("y", 0.0), t,
                            str(e.get("layer", ""))))
    return out


def main():
    path = sys.argv[1]
    entities = read(path)
    summarise(entities)

    geo = geometry(entities)
    print("\nshape entities: %d" % len(geo))
    by_layer = defaultdict(Counter)
    for e in geo:
        by_layer[str(e.get("layer", ""))][e["type"]] += 1
    for lay, c in sorted(by_layer.items(), key=lambda kv: -sum(kv[1].values())):
        print("   layer %-24s %s" % (lay, dict(c)))

    tx = texts(entities)
    print("\n%d text entities" % len(tx))
    for x, y, t, lay in tx[:60]:
        print("   (%9.3f, %9.3f) [%-14s] %s" % (x, y, lay, t[:70]))

    circles = [e for e in geo if e["type"] == "CIRCLE"]
    if circles:
        print("\ncircles, by diameter:")
        for d, group in sorted(Counter(round(2 * e["r"], 3)
                                       for e in circles).items()):
            print("   O%-10.3f x%d" % (d, group))
    arcs = [e for e in geo if e["type"] == "ARC"]
    if arcs:
        print("\narc radii: %s"
              % ", ".join("R%.3f x%d" % (r, n) for r, n in
                          sorted(Counter(round(e["r"], 3)
                                         for e in arcs).items())[:20]))


if __name__ == "__main__":
    main()
