"""Panel drill template for a pedal board, from the board's own placement data (its gen_<board>.py design).

    python drill_template.py <board dir> <generator module>      e.g. gain-compressor gen_gate_comp

Writes <name>_drill.pdf next to the board: A4 portrait at 1:1 (print at 100 %, no "fit to page"), a 50 mm scale
bar to check the print, the board outline as a reference and a cross on each hole to drill in the lid, seen from
outside (the board's top side faces the lid, so the view is not mirrored). Hole sizes:

  encoder bushing (PEC11R, M7 x 0.75 thread)   7.5 mm
  footswitch (12 mm, M12 thread)              12.5 mm (ISO 273 fine)
  on-board LED window (optional)               3 mm, drawn dashed

The template is centred on the board: line the dashed centre lines up with the lid's centre lines.
"""
import importlib
import math
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HOLES = {"ENC": (7.5, "solid"), "HOLE12": (12.5, "solid"), "LED": (3.0, "dashed")}
NAMES = {"HOLE12": "footswitch", "LED": "LED"}


def holes(d):
    out = []
    for p in sorted(d.parts.values(), key=lambda p: p.ref):
        if p.kind in HOLES:
            dia, style = HOLES[p.kind]
            out.append((p.ref, p.silk or NAMES.get(p.kind, p.kind), p.x, p.y, dia, style))
    return out


def outline_paths(d):
    """The board outline as point lists (template outline: lines, arcs as polylines, circles)."""
    if not d.outline:
        w, h = d.width, d.height
        return [[(0, 0), (w, 0), (w, h), (0, h), (0, 0)]]
    from gen.outline import _arc_points
    paths = []
    for kind, ps in d.outline.items:
        if kind == "line":
            paths.append(ps)
        elif kind == "arc":
            paths.append(_arc_points(*ps))
        else:
            (cx, cy), e = ps
            r = math.dist(ps[0], e)
            paths.append([(cx + r * math.cos(a / 36 * math.pi), cy + r * math.sin(a / 36 * math.pi)) for a in range(73)])
    return paths


def write(d, pdf):
    W, H = 210.0, 297.0
    fig = plt.figure(figsize=(W / 25.4, H / 25.4))
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)                                   # y down, like the board
    ax.set_aspect("equal")
    ax.axis("off")
    ox, oy = W / 2 - d.width / 2, H / 2 - d.height / 2 + 10
    for path in outline_paths(d):
        ax.plot([ox + x for x, _ in path], [oy + y for _, y in path], color="0.6", lw=0.4)
    cx, cy = ox + d.width / 2, oy + d.height / 2
    ax.plot([cx, cx], [oy - 8, oy + d.height + 8], color="0.5", lw=0.3, ls=(0, (6, 3)))
    ax.plot([ox - 8, ox + d.width + 8], [cy, cy], color="0.5", lw=0.3, ls=(0, (6, 3)))
    for ref, label, x, y, dia, style in holes(d):
        X, Y = ox + x, oy + y
        ax.add_patch(plt.Circle((X, Y), dia / 2, fill=False, lw=0.4, ls=style))
        ax.plot([X - dia / 2 - 2, X + dia / 2 + 2], [Y, Y], color="k", lw=0.2)
        ax.plot([X, X], [Y - dia / 2 - 2, Y + dia / 2 + 2], color="k", lw=0.2)
        ax.text(X + dia / 2 + 1, Y - dia / 2 - 0.5, f"{label} {dia:g}", fontsize=5)
        ax.text(X + dia / 2 + 1, Y + dia / 2 + 1.8, f"x{x - d.width / 2:+.1f} y{y - d.height / 2:+.1f}", fontsize=4,
                color="0.3")
    # scale bar and title
    ax.plot([20, 70], [H - 20, H - 20], color="k", lw=0.8)
    for x in (20, 70):
        ax.plot([x, x], [H - 22, H - 18], color="k", lw=0.8)
    ax.text(45, H - 23, "50 mm - check the print scale", fontsize=6, ha="center")
    ax.text(20, 15, f"{d.title} Rev{d.rev} - lid drill template, 1:1, view from outside", fontsize=8)
    ax.text(20, 20, "Dashed lines: centre lines of the board (= of the enclosure). Positions relative to the centre.",
            fontsize=5)
    ax.text(20, 24, "RJ45: side wall at the board's top edge (not on the lid). Dashed circles: optional LED windows.",
            fontsize=5)
    fig.savefig(pdf, metadata={"CreationDate": None, "Producer": None, "Creator": None})
    plt.close(fig)


def main():
    board_dir, module = os.path.abspath(sys.argv[1]), sys.argv[2]
    sys.path.insert(0, board_dir)
    sys.path.insert(0, os.path.join(os.path.dirname(board_dir), "common"))
    d = importlib.import_module(module).design()
    pdf = os.path.join(board_dir, d.name + "_drill.pdf")
    write(d, pdf)
    for h in holes(d):
        print(f"  {h[0]:5} {h[1]:11} x={h[2] - d.width / 2:+6.1f} y={h[3] - d.height / 2:+6.1f}  {h[4]:g} mm")
    print("written", pdf)


if __name__ == "__main__":
    main()
