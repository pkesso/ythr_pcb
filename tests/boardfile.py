"""Reading the generated KiCad files for the tests: the board (footprints, pads, tracks, vias, texts, outline) as
KiCad wrote it, and the board designs (gen_<board>.py) as the generator describes them."""
import csv
import importlib
import math
import os
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
PCB = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(PCB, "tools"))
sys.path.insert(0, os.path.join(PCB, "common"))
from kicad_gen import sexpr, kids, kid  # noqa: E402

# (directory, generator module, project name)
BOARDS = [("effect", "gen_effect", "effect"), ("gain-compressor", "gen_gate_comp", "gate_comp"),
          ("echo", "gen_echo", "echo"), ("rever", "gen_reverb", "reverb"), ("presets", "gen_presets", "presets"),
          ("main", "gen_main", "main")]

OX, OY = 100.0, 100.0          # gen.board writes board coordinates with this offset


def design(d):
    """The Design object of a board, from its generator script."""
    path = os.path.join(PCB, d[0])
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module(d[1]).design()


def path(d, suffix):
    return os.path.join(PCB, d[0], d[2] + suffix)


def _num(node, key, i=1, default=None):
    k = kid(node, key)
    return float(k[i]) if k and len(k) > i else default


class Pad:
    def __init__(self, fp, node):
        self.ref = fp.ref
        self.num = node[1]
        self.kind, self.shape = node[2], node[3]
        at = kid(node, "at")
        lx, ly = float(at[1]), float(at[2])
        a = math.radians(fp.rot)
        # pad position in the file is footprint-local; KiCad rotates clockwise on screen for +angle (Y down)
        self.x = fp.x + lx * math.cos(a) + ly * math.sin(a)
        self.y = fp.y - lx * math.sin(a) + ly * math.cos(a)
        size = kid(node, "size")
        self.w, self.h = float(size[1]), float(size[2])
        drill = kid(node, "drill")
        self.drill = None
        if drill:
            vals = [float(v) for v in drill[1:] if not isinstance(v, list) and v != "oval"]
            self.drill = min(vals) if vals else None
        net = kid(node, "net")
        self.net = net[-1] if net else None


class FootprintInfo:
    def __init__(self, node):
        self.lib_id = node[1]
        self.layer = kid(node, "layer")[1]
        at = kid(node, "at")
        self.x, self.y = float(at[1]) - OX, float(at[2]) - OY
        self.rot = float(at[3]) if len(at) > 3 else 0.0
        self.props = {p[1]: p[2] for p in kids(node, "property")}
        self.ref = self.props.get("Reference")
        attr = kid(node, "attr")
        self.attr = set(attr[1:]) if attr else set()
        self.pads = [Pad(self, p) for p in kids(node, "pad")]
        self.texts = []
        for p in kids(node, "property"):
            lay = kid(p, "layer")
            if lay and "SilkS" in lay[1] and not kid(p, "hide"):
                self.texts.append((p[2], lay[1], _font(p)))


def _font(node):
    eff = kid(node, "effects")
    font = kid(eff, "font") if eff else None
    size = kid(font, "size") if font else None
    th = kid(font, "thickness") if font else None
    return (float(size[1]) if size else 1.27, float(th[1]) if th else None)


class Board:
    def __init__(self, file):
        with open(file, encoding="utf-8") as fh:
            t = sexpr(fh.read())
        self.footprints = [FootprintInfo(n) for n in kids(t, "footprint")]
        self.segments = [dict(w=_num(s, "width"), layer=kid(s, "layer")[1],
                              x1=float(kid(s, "start")[1]) - OX, y1=float(kid(s, "start")[2]) - OY,
                              x2=float(kid(s, "end")[1]) - OX, y2=float(kid(s, "end")[2]) - OY)
                         for s in kids(t, "segment")]
        self.vias = [dict(size=_num(v, "size"), drill=_num(v, "drill"),
                          x=float(kid(v, "at")[1]) - OX, y=float(kid(v, "at")[2]) - OY) for v in kids(t, "via")]
        self.texts = [(g[1], kid(g, "layer")[1], _font(g)) for g in kids(t, "gr_text")]
        rect = [g for g in kids(t, "gr_rect") if kid(g, "layer")[1] == "Edge.Cuts"]
        if rect:
            s, e = kid(rect[0], "start"), kid(rect[0], "end")
            self.outline = (float(s[1]) - OX, float(s[2]) - OY, float(e[1]) - OX, float(e[2]) - OY)
        # an outline from an enclosure template: lines and arcs (outline = their bounding box), circles = holes
        self.edges = [g for g in t if isinstance(g, list) and g[0] in ("gr_line", "gr_arc", "gr_circle")
                      and kid(g, "layer")[1] == "Edge.Cuts"]
        if not rect:
            pts = [(float(kid(g, k)[1]) - OX, float(kid(g, k)[2]) - OY) for g in self.edges if g[0] != "gr_circle"
                   for k in ("start", "mid", "end") if kid(g, k)]
            self.outline = (min(x for x, _ in pts), min(y for _, y in pts), max(x for x, _ in pts),
                            max(y for _, y in pts))
        self.zones = kids(t, "zone")

    def fp(self, ref):
        return next(f for f in self.footprints if f.ref == ref)


def read_csv(file):
    with open(file, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))
