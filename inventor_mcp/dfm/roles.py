"""What a parameter means to the DFM analyser.

Its own module because two things need it and neither should import the other:
the recipe schema, to validate a declaration, and the remediation rules, to act
on one. Nothing here imports anything, which is what keeps that true.

A role is a claim about a parameter -- "this one is the nominal wall" -- and it
is declared rather than guessed. Several of the tool's checks are judged on
numbers a person types into a panel, and the model already knows every one of
them exactly, so declaring the map means those checks run against the part
rather than against somebody's recollection of it. Guessing the map from
spellings would put a loop one plausible name away from thinning the wrong wall.
"""

from __future__ import annotations

#: role -> (the DFM setting it supplies, what it is)
ROLES: dict[str, tuple[str, str]] = {
    "wall": ("wallThk", "the nominal wall thickness"),
    "draft": ("draftAngle", "the draft angle on the side walls"),
    "rib_thickness": ("ribThk", "the thickness of a rib"),
    "rib_height": ("ribH", "the height of a rib above the wall"),
    "rib_fillet": ("ribRadius", "the fillet at a rib's root"),
    "boss_od": ("bossOD", "a boss's outside diameter"),
    "boss_wall": ("bossWall", "the wall thickness of a boss"),
}

ROLE_NAMES = tuple(sorted(ROLES))
