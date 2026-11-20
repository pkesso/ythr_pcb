"""Build one board project from its Design: schematic, routed board, project files, BOM/CPL, checks, renders.

    build(design, out_dir)      called by <board>/gen_<board>.py
"""
import os
import pickle
import sys
import time
import uuid

from . import schematic
from .board import Board, RIM_GAP, project_files
from .libs import PCB
sys.path.insert(0, os.path.join(PCB, "tools"))
import kicad_cli  # noqa: E402
import kicad_checks  # noqa: E402

NS = uuid.UUID("2f0c9a54-71d3-4d8e-b0a1-5c3e9e7d6b21")


def schematic_nets(sch):
    """{(ref, pin): net name} as KiCad names the nets of the schematic."""
    import tempfile
    from kicad_gen import sexpr, kids, kid
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "n.net")
        kicad_cli.run("sch", "export", "netlist", "--format", "kicadsexpr", "-o", out, sch)
        t = sexpr(open(out, encoding="utf-8").read())
    res = {}
    for net in kids(kid(t, "nets"), "net"):
        name = kid(net, "name")[1]
        for node in kids(net, "node"):
            res[(kid(node, "ref")[1], kid(node, "pin")[1])] = name
    return res


def build(design, out_dir, checks=True, render=True, reroute=False):
    t0 = time.time()
    name = design.name
    root = str(uuid.uuid5(NS, name))
    os.makedirs(out_dir, exist_ok=True)
    sch = os.path.join(out_dir, name + ".kicad_sch")
    pcb = os.path.join(out_dir, name + ".kicad_pcb")
    project_files(out_dir, name, root, jlc_rules=design.jlc_rules)
    schematic.write(design, sch, root)
    kicad_cli.upgrade("sch", sch)
    print(f"[{name}] schematic written")

    b = Board(design, out_dir, root, netlist=schematic_nets(sch))
    if design.router == "freerouting":
        return _build_freerouted(design, b, out_dir, sch, pcb, t0, checks, render, reroute)
    # routing result cache: keyed by the placement + nets, so an unchanged design is not rerouted
    key = repr(sorted((p.ref, p.kind, p.x, p.y, p.rot, p.side, sorted(p.pins.items(), key=lambda kv: kv[0]),)
                      for p in design.parts.values())) + repr(design.keepouts) + repr(design.keep_circles) + repr((design.width, design.height)) + (repr(design.outline.items) if design.outline else "")
    if design.outline and design.rim:           # the rim keeps the copper in (board.RIM_GAP)
        key += repr(("rim", design.outline.frame, RIM_GAP))
    cache = os.path.join(out_dir, "routing.pkl")
    cached = None
    if os.path.exists(cache) and not reroute:
        with open(cache, "rb") as fh:
            data = pickle.load(fh)
        if data.get("key") == key:
            cached = data
    if cached:
        from .router import Router
        r = Router.__new__(Router)
        r.segments, r.vias = cached["segments"], cached["vias"]
        b.router, b.failed = r, cached["failed"]
        print(f"[{name}] routing taken from routing.pkl ({len(r.segments)} segments, {len(r.vias)} vias)")
    else:
        b.route()
        with open(cache, "wb") as fh:
            pickle.dump({"key": key, "segments": b.router.segments, "vias": b.router.vias, "failed": b.failed}, fh)
    if b.failed:
        print(f"[{name}] UNROUTED: {b.failed}")
    missing = b.place_labels()
    if missing:
        print(f"[{name}] labels without room on silkscreen (moved to Fab): {[(m[0], m[1]) for m in missing]}")
    b.write(pcb)
    kicad_cli.upgrade("pcb", pcb)
    return _finish(design, b, out_dir, sch, pcb, t0, checks, render, missing)


def _build_freerouted(design, b, out_dir, sch, pcb, t0, checks, render, reroute):
    """Board written without tracks, then routed by tools/freeroute.py (Freerouting; it also stitches the GND zones
    and keys the new UUIDs by geometry). The routed file is cached in routing.pkl, keyed by the unrouted board's bytes
    and the options, so an unchanged board is not routed again and regenerates byte-identically."""
    import hashlib
    from .router import Router
    name = design.name
    r = Router.__new__(Router)
    r.segments, r.vias = [], []
    b.router, b.failed = r, []
    missing = b.place_labels()
    if missing:
        print(f"[{name}] labels without room on silkscreen (moved to Fab): {[(m[0], m[1]) for m in missing]}")
    b.write(pcb)
    kicad_cli.upgrade("pcb", pcb)
    args = ["--silk-keepouts"] * bool(design.outline and design.rim) + list(design.freeroute_args)
    with open(pcb, "rb") as fh:
        key = hashlib.sha256(fh.read() + repr(args).encode()).hexdigest()
    cache = os.path.join(out_dir, "routing.pkl")
    data = {}
    if os.path.exists(cache) and not reroute:
        with open(cache, "rb") as fh:
            data = pickle.load(fh)
    if data.get("freerouting") == key:
        with open(pcb, "wb") as fh:
            fh.write(data["board"])
        print(f"[{name}] routing taken from routing.pkl (Freerouting)")
    else:
        print(f"[{name}] routing with Freerouting ({' '.join(args) or 'default options'})")
        cmd = [sys.executable, "-u", os.path.join(PCB, "tools", "freeroute.py"), pcb, *args]
        code = kicad_cli.run_process(cmd, timeout=1800, echo="  freeroute: ", encoding="utf-8",
                                     errors="replace").returncode
        if code:
            b.failed = ["freeroute.py exit code %d" % code]
        else:
            with open(pcb, "rb") as fh:
                routed = fh.read()
            with open(cache, "wb") as fh:
                pickle.dump({"freerouting": key, "board": routed}, fh)
    if b.failed:
        print(f"[{name}] ROUTING FAILED: {b.failed}")
    return _finish(design, b, out_dir, sch, pcb, t0, checks, render, missing)


def _finish(design, b, out_dir, sch, pcb, t0, checks, render, missing):
    name = design.name
    b.assembly(os.path.join(out_dir, name + "_bom.csv"), os.path.join(out_dir, name + "_cpl.csv"))
    print(f"[{name}] board written ({time.time() - t0:.0f} s)")
    result = {"failed": b.failed, "labels_missing": missing}
    if checks:
        erc = kicad_checks.summarise(kicad_checks.erc(sch))
        drc = kicad_checks.summarise(kicad_checks.drc(pcb))
        for title, groups in (("ERC", erc), ("DRC", drc)):
            n_err = sum(len(v) for (s, _), v in groups.items() if s == "error")
            n_warn = sum(len(v) for (s, _), v in groups.items() if s != "error")
            print(f"[{name}] {title}: {n_err} errors, {n_warn} warnings")
            for (sev, typ), vs in groups.items():
                print(f"      {sev:7} {typ:34} x{len(vs)}   e.g. {kicad_checks.describe(vs[0])[:160]}")
        result["erc"], result["drc"] = erc, drc
    if render:
        for side in ("top", "bottom"):
            kicad_cli.run("pcb", "render", "--side", side, "-w", "1400", "-h", "1400", "--zoom", "0.9",
                          "--background", "opaque", "-o", os.path.join(out_dir, f"{name}_{side}.png"), pcb)
        print(f"[{name}] renders: {name}_top.png, {name}_bottom.png")
    return result
