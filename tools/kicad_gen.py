"""Building blocks for Python generators that write KiCad files.

    from kicad_gen import Uuids, sexpr, dump, q, f, flip_layer, load_footprint, board_footprint_items

- Uuids: UUIDs derived from what an item is (uuid5 of a key), never from write order. KiCad sorts items by UUID when it
  saves, so sequential UUIDs make every insertion reorder the whole file and produce enormous diffs.
- sexpr / dump: parse and write S-expressions; keep_quotes=True keeps string tokens exactly as written.
- load_footprint: a library .kicad_mod as raw tree + pads + courtyard bounding box.
- board_footprint_items: a library footprint's items as KiCad stores them inside a board footprint (mirrored for the
  bottom side, absolute pad/text angles, keyed UUIDs). Copying the library footprint whole keeps KiCad's
  lib_footprint_mismatch check at zero.
- stable_library_uuids: after `kicad-cli fp upgrade` of a generated or copied .pretty, replace the random UUIDs KiCad
  gave to items without one (library properties) by keyed ones, so rebuilding the library gives identical files.
"""
import copy
import math
import os
import re
import uuid


# ---------------------------------------------------------------- UUIDs
class Uuids:
    """Deterministic UUIDs: one namespace per generated file, one key per item.

    ids = Uuids("3b1a7c55-0f7e-4d0c-9a43-6f1b2a9d8e10")
    ids("R5")                                   # footprint
    ids("R5", "pad", "1")                       # pad
    ids("segment", "B.Cu", 10.5, 20, 12, 20)    # track segment: identified by its geometry
    ids("label", "R5", 1, "2")                  # schematic label: by the symbol pin it belongs to

    A key must name the item, not its position in the output. A duplicate key raises: two items with the same UUID
    would make KiCad replace one of them with a random UUID on the next save.
    """

    def __init__(self, namespace):
        self.ns = uuid.UUID(str(namespace))
        self.used = set()

    def __call__(self, *key):
        k = "/".join(map(str, key))
        if k in self.used:
            raise ValueError(f"duplicate UUID key {k!r}")
        self.used.add(k)
        return str(uuid.uuid5(self.ns, k))

    def q(self, *key):
        """The UUID quoted, ready for `(uuid ...)`."""
        return '"' + self(*key) + '"'


# ---------------------------------------------------------------- S-expressions
_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\(|\)|[^\s()"]+')


def sexpr(text, keep_quotes=False):
    """Parse an S-expression into nested lists. keep_quotes keeps string tokens quoted, so dump() can write them back."""
    stack = [[]]
    for t in _TOKEN.findall(text):
        if t == "(":
            stack.append([])
        elif t == ")":
            node = stack.pop()
            stack[-1].append(node)
        else:
            stack[-1].append(t[1:-1] if t.startswith('"') and not keep_quotes else t)
    return stack[0][0]


def dump(node):
    """Inverse of sexpr(..., keep_quotes=True), on one line (kicad-cli upgrade reformats it)."""
    return "(" + " ".join(dump(x) if isinstance(x, list) else x for x in node) + ")"


def kids(node, key):
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


def kid(node, key):
    k = kids(node, key)
    return k[0] if k else None


def q(s):
    """A KiCad string literal."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def f(v):
    """A coordinate, at most 4 decimals, no trailing zeros. Not `{:g}`: its 6 significant digits cut 123.4567 to
    123.457."""
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# ---------------------------------------------------------------- layers
_FLIP = {"F.Cu": "B.Cu", "F.Mask": "B.Mask", "F.Paste": "B.Paste", "F.SilkS": "B.SilkS", "F.Fab": "B.Fab",
         "F.CrtYd": "B.CrtYd", "F.Adhes": "B.Adhes"}
_FLIP.update({v: k for k, v in _FLIP.items()})


def flip_layer(layer):
    return _FLIP.get(layer, layer)


# ---------------------------------------------------------------- library footprints
class Footprint:
    def __init__(self, raw, pads, courtyard, attr):
        self.raw = raw              # parsed with keep_quotes=True
        self.pads = pads            # [(number, type, shape, x, y, w, h, drill)] in footprint coordinates; see drill_size
        self.courtyard = courtyard  # (x0, y0, x1, y1) in footprint coordinates, or None
        self.attr = attr            # "smd" / "through_hole" / ...


def drill_size(pad):
    """Hole diameter of a pad, the width for an oval hole; None without a hole (a (drill (offset ...)) has no size)."""
    sizes = [float(v) for v in (kid(pad, "drill") or [])[1:] if not isinstance(v, list) and v != "oval"]
    return min(sizes) if sizes else None


def load_footprint(path):
    """A library footprint (.kicad_mod) with its pads and courtyard bounding box."""
    text = open(path, encoding="utf-8").read()
    t = sexpr(text)
    pads = []
    for p in kids(t, "pad"):
        at, size = kid(p, "at"), kid(p, "size")
        pads.append((p[1], p[2], p[3], float(at[1]), float(at[2]), float(size[1]), float(size[2]), drill_size(p)))
    pts = courtyard_points(t)
    court = (min(x for x, _ in pts), min(y for _, y in pts), max(x for x, _ in pts), max(y for _, y in pts)) if pts else None
    attr = kid(t, "attr")
    return Footprint(sexpr(text, keep_quotes=True), pads, court, attr[1] if attr else "through_hole")


def courtyard_points(footprint):
    """Points bounding the courtyard of a parsed footprint, in footprint coordinates."""
    pts = []
    for g in footprint:
        if not (isinstance(g, list) and g[0] in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc")):
            continue
        layer = kid(g, "layer")
        if not layer or not layer[1].endswith("CrtYd"):
            continue
        if g[0] == "fp_circle":
            c, e = kid(g, "center"), kid(g, "end")
            cx, cy = float(c[1]), float(c[2])
            r = math.dist((cx, cy), (float(e[1]), float(e[2])))
            pts += [(cx - r, cy - r), (cx + r, cy + r)]
        elif g[0] == "fp_poly":
            pts += [(float(xy[1]), float(xy[2])) for xy in kids(kid(g, "pts"), "xy")]
        else:
            pts += [(float(n[1]), float(n[2])) for n in (kid(g, "start"), kid(g, "mid"), kid(g, "end")) if n]
    return pts


# items of a library footprint the generator writes itself (header and fields)
LIB_SKIP = {"version", "generator", "generator_version", "layer", "property"}
_COORDS = {"at", "start", "mid", "end", "center", "xy"}
_ANGLED = {"pad", "fp_text"}  # their (at x y angle) holds the absolute orientation on the board
_CHAMFER = {"top_left": "bottom_left", "top_right": "bottom_right", "bottom_left": "top_left", "bottom_right": "top_right"}


def board_item(item, side, rot, uuids, key, extra=()):
    """One library footprint item as KiCad stores it on the board.

    side: "F" or "B"; rot: footprint rotation in degrees; key: identity of the item (e.g. (ref, index));
    extra: nodes appended to the item, e.g. [["net", "3", '"/VCC"']] for a pad.
    Bottom side: Y is mirrored by negating the coordinate text (not a float - library values can carry more
    digits than a reformatted number, and KiCad compares them exactly), together with drill offsets and trapezoid
    deltas; pad chamfers swap top and bottom, F/B layers swap, text gets justify mirror.
    """
    item = copy.deepcopy(item)
    bottom = side == "B"
    count = [0]

    def walk(node, parent):
        head = node[0]
        if bottom and (head in _COORDS or head == "rect_delta" or (head == "offset" and parent == "drill")):
            y = node[2]
            node[2] = y[1:] if y.startswith("-") else y if float(y) == 0 else "-" + y
        if head == "chamfer" and bottom:
            node[1:] = [_CHAMFER[c] for c in node[1:]]
        if head == "at" and parent in _ANGLED:
            a = float(node[3]) if len(node) > 3 else 0.0
            a = ((-a if bottom else a) + rot) % 360
            del node[3:]
            if a:
                node.append(f(a))
        if head in ("layer", "layers") and bottom:
            node[1:] = [q(flip_layer(t.strip('"'))) for t in node[1:]]
        if head == "uuid":
            count[0] += 1
            node[1] = uuids.q(*key, count[0])
        if head == "effects" and bottom:
            j = kid(node, "justify")
            if j is None:
                node.append(["justify", "mirror"])
            elif "mirror" not in j:
                j.append("mirror")
        for c in node[1:]:
            if isinstance(c, list):
                walk(c, head)

    walk(item, None)
    item.extend(extra)
    # library items without a UUID would get a random one from KiCad on every save
    if item[0] in ("pad", "point") or item[0].startswith("fp_"):
        if not kid(item, "uuid"):
            item.append(["uuid", uuids.q(*key)])
    return dump(item)


def board_footprint_items(fp, ref, side, rot, uuids, pad_extra=lambda number: ()):
    """All items of a library footprint for the board footprint `ref` (fields excluded: write those yourself so
    they match the schematic symbol). pad_extra(number) returns extra nodes for a pad, typically its net."""
    out = []
    for i, item in enumerate(fp.raw[2:]):
        if isinstance(item, list) and item[0] not in LIB_SKIP:
            extra = pad_extra(item[1].strip('"')) if item[0] == "pad" else ()
            out.append(board_item(item, side, rot, uuids, (ref, i), list(extra)))
    return out


def stable_library_uuids(pretty, namespace):
    """Make every UUID in the footprints of a .pretty directory deterministic: uuid5(namespace, "<file>/<n>") for the
    n-th UUID in the file. Run it right after kicad_cli.upgrade("fp", pretty): KiCad's library footprints have no
    UUIDs on their properties (Reference, Value, Datasheet, ...), and upgrade gives each a random one on every run,
    so a library snapshot copied from KiCad would show a diff in every .kicad_mod each time it is rebuilt.
    Keyed by position in the file: stable as long as the footprint itself does not change."""
    ns = uuid.UUID(str(namespace))
    for fn in sorted(os.listdir(pretty)):
        if not fn.endswith(".kicad_mod"):
            continue
        path = os.path.join(pretty, fn)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        n = iter(range(10 ** 9))
        text = re.sub(r'\(uuid "[0-9a-fA-F-]{36}"\)',
                      lambda m: f'(uuid "{uuid.uuid5(ns, fn + "/" + str(next(n)))}")', text)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)


def to_board(px, py, x, y, rot, side):
    """Footprint-local point -> board coordinates (rotation in multiples of 90 degrees)."""
    if side == "B":
        py = -py
    a = math.radians(rot)
    c, s = round(math.cos(a)), round(math.sin(a))
    return x + px * c + py * s, y - px * s + py * c
