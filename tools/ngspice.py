"""ngspice as a shared library, using the copy that ships with KiCad (no separate install needed).

    from ngspice import Ngspice
    ng = Ngspice()                                   # finds KiCad's ngspice through kicad_cli.kicad_bin()
    r = ng.run(netlist_lines, ["frequency", "out"])  # netlist ends with an analysis line and ".end"
    gain_db = 20 * numpy.log10(abs(r["out"]))

Audio or any other arbitrary input: declare the source as EXTERNAL and give a function of time.
ngspice asks for its value at every time point, so there is no giant PWL table in the netlist.

    ng.external = lambda t: numpy.interp(t, t_samples, volts, left=0, right=0)
    lines = [..., "V1 IN 0 DC 0 EXTERNAL", ".tran 22.7u 3.3 0 22.7u", ".end"]
    r = ng.run(lines, ["time", "out"])               # resample r["out"] onto the sample grid with numpy.interp

Notes
- Vector names are lower-case node names ("out", "v(out)" also works); "time" / "frequency" are the sweep vectors.
- ngspice reports problems through its text output; run() raises if a line contains "error" or says that source
  stepping failed (the operating point is then wrong, and ngspice carries on without reporting an error).
- KiCad's ngspice.dll prints "can't find the initialization file spinit": harmless, XSPICE code models are not used.
- One Ngspice() per process: the shared library keeps global state.
"""
import ctypes as C
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kicad_cli  # noqa: E402

FAILURES = ("error", "source stepping failed")  # lower case, looked for in ngspice's output


class _VecInfo(C.Structure):
    _fields_ = [("vname", C.c_char_p), ("vtype", C.c_int), ("vflags", C.c_short),
                ("vrealdata", C.POINTER(C.c_double)), ("vcompdata", C.POINTER(C.c_double)), ("vlength", C.c_int)]


def library_path():
    """KiCad's ngspice shared library."""
    name = {"nt": "ngspice.dll"}.get(os.name, "libngspice.so")
    if sys.platform == "darwin":
        name = "libngspice.dylib"
    return kicad_cli.kicad_file(name)


class Ngspice:
    def __init__(self, path=None):
        path = path or library_path()
        if os.name == "nt":
            os.add_dll_directory(os.path.dirname(path))
        self.lib = C.CDLL(path)
        self.log = []
        self.external = None  # f(time) -> volts for sources declared EXTERNAL
        out = C.CFUNCTYPE(C.c_int, C.c_char_p, C.c_int, C.c_void_p)
        stat = C.CFUNCTYPE(C.c_int, C.c_char_p, C.c_int, C.c_void_p)
        exit_ = C.CFUNCTYPE(C.c_int, C.c_int, C.c_bool, C.c_bool, C.c_int, C.c_void_p)
        self._cb = (out(self._print), stat(lambda *a: 0), exit_(lambda *a: 0))
        self.lib.ngSpice_Init(*self._cb, None, None, None, None)
        self.lib.ngGet_Vec_Info.restype = C.POINTER(_VecInfo)
        src = C.CFUNCTYPE(C.c_int, C.POINTER(C.c_double), C.c_double, C.c_char_p, C.c_int, C.c_void_p)
        sync = C.CFUNCTYPE(C.c_int, C.c_double, C.POINTER(C.c_double), C.c_double, C.c_int, C.c_int, C.c_int, C.c_void_p)
        self._sync_cb = (src(self._vsrc), src(lambda *a: 0), sync(lambda *a: 0))
        self._ident = C.c_int(0)
        self.lib.ngSpice_Init_Sync(*self._sync_cb, C.byref(self._ident), None)

    def _print(self, s, _id, _user):
        self.log.append(s.decode(errors="replace"))
        return 0

    def _vsrc(self, value, t, _node, _id, _user):
        value[0] = float(self.external(t)) if self.external else 0.0
        return 0

    def run(self, lines, vectors):
        """Load the netlist (a list of lines ending with '.end'), run its analysis, return {name: numpy array}.
        AC results are complex."""
        self.log.clear()
        enc = [l.encode() for l in lines] + [None]
        try:
            self.lib.ngSpice_Circ((C.c_char_p * len(enc))(*enc))
            self.lib.ngSpice_Command(b"run")
            if any(word in l.lower() for l in self.log for word in FAILURES):
                raise RuntimeError("ngspice:\n" + "\n".join(self.log))
            return {name: self._vec(name) for name in vectors}
        finally:
            self.lib.ngSpice_Command(b"remcirc")
            self.lib.ngSpice_Command(b"destroy all")

    def _vec(self, name):
        p = self.lib.ngGet_Vec_Info(name.encode())
        if not p:
            raise KeyError(f"no vector {name!r} (node names are lower case)")
        v = p.contents
        if v.vcompdata:
            a = np.ctypeslib.as_array(v.vcompdata, (v.vlength * 2,))
            return a[0::2] + 1j * a[1::2]
        return np.ctypeslib.as_array(v.vrealdata, (v.vlength,)).copy()


if __name__ == "__main__":  # smoke test: RC low-pass, -3 dB at ~159 Hz
    ng = Ngspice()
    r = ng.run(["rc", "V1 a 0 DC 0 AC 1", "R1 a b 1k", "C1 b 0 1u", ".ac dec 20 10 10k", ".end"], ["frequency", "b"])
    attenuation = -20 * np.log10(np.abs(r["b"]))  # grows with frequency
    f3 = np.interp(3.0103, attenuation, r["frequency"].real)
    print(f"RC low-pass -3 dB at {f3:.0f} Hz (expected ~159 Hz)")
    ng.external = lambda t: 0.5 * np.sin(2 * np.pi * 1000 * t)
    r = ng.run(["ext", "V1 a 0 DC 0 EXTERNAL", "R1 a b 1k", "C1 b 0 1u", ".tran 10u 5m 0 10u", ".end"], ["time", "a"])
    print(f"EXTERNAL source peak {np.max(r['a']):.3f} V (expected 0.500)")
