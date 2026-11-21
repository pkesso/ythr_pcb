# Boards — status (rev A, generated)

Decisions: [README.md](README.md). Generator: [common/README.md](common/README.md).

| Board | Dir | Size | Colour | Addr (jumpers, factory) | Encoders | Indicators | Wire pads | ERC / DRC |
|---|---|---|---|---|---|---|---|---|
| Effect (**Rev0**, see [effect/REPORT.md](effect/REPORT.md)) | `effect/` | 50 × 104, Type_Z-76_Z-77 template | purple | 0x23 (A0=1 A1=1 A2=0) | FDBK, DEPTH, MIX (centre), PR_DL, SPEED | CH FL PH TR | MODE, ON (debounced, Q27 buttons); 6 test pads | 0 / 0 |
| Gate + Comp (**Rev0**, see [gain-compressor/REPORT.md](gain-compressor/REPORT.md)) | `gain-compressor/` | 60 × 116, 1590N1 template | yellow | 0x22 (A0=0 A1=1 A2=0) | THRESHOLD, LEVEL, DECAY, SUSTAIN | GATE, COMP | SW, SW (debounced); 6 test pads | 0 / 0 |
| Echo | `echo/` | 50 × 65 | white, black silk | 0x24 (A0=0 A1=0 A2=1) | FDBK, TIME (push = TAP), MIX (centre), BASS, TREBLE | TAPE, DIGI | MODE, TAP, ON | 0 / 0 |
| Reverb | `rever/` | 50 × 65 | blue | 0x25 (A0=1 A1=0 A2=1) | DECAY, PR_DL, TONE, MIX | SP RM PT HL | MODE, ON | 0 / 0 |
| Presets | `presets/` | 200 × 50 | black | 0x26 (A0=0 A1=1 A2=1) | — | — (none) | SW1…SW5 | 0 / 0 |
| Main | `main/` | 180 × 110 | green | 0x20 (0 0 0) + 0x21 (A0=1) | GAIN, MASTER, BASS, MID, TREBLE / AMP, CAB, GUITAR, AUDIO | BOUTIQUE (blue), MODERN (green), CLASSIC (orange), battery 1–4 (red); 7-seg 2 digits | FLAVOR (PREV, GND, NEXT) | 0 / 0 |

DRC includes schematic parity and refilled zones; KiCad 10.0.2. Each board regenerates byte-identically (checked).

## Mechanics (as generated)

- Top edge = RJ45 side; the jack is on the **bottom** side, plug opening flush with the top edge.
- Encoders (Bourns PEC11R-4015F-S0024, push switch unconnected except Echo TIME = TAP) on the top side, 28 mm
  square (centre encoder for 5-knob pedals); pins face the centre column. The panel rests on the encoder
  bushings, 6 mm above the board.
- Gate + Comp and Effect hang on their two Dailywell Q27 push buttons soldered into the board instead (lid on their housings,
  10.43 mm above the board; the encoders only pass through 7.5 mm lid holes), shielded RJ45 with power / INT LEDs,
  rim 0.3 mm: [gain-compressor/REPORT.md](gain-compressor/REPORT.md), [effect/REPORT.md](effect/REPORT.md). Effect:
  encoders on a 30 mm square on the side lines x = 10 / 40 (MIX in the centre), indicators at y = 65, buttons at
  y = 87.
- On-board LEDs + their jumpers in a row 3 mm above the bottom edge; the bezel lamps' wire pads (`+` = GPIO,
  `−` = GND) below each LED, footswitch wire pads in the bottom corners (GPIO, GND).
- Presets hangs on its five footswitches: 12.5 mm holes (M12 clearance, ISO 273 fine) at y = 36 mm, 45 mm
  pitch, the third in the centre (x = 10, 55, 100, 145, 190 mm), with a 20 mm nut area kept free of parts and
  tracks; above each a plain 18 mm hole, its edge 1 mm from the nut area (centre y = 16 mm). No indicators on
  Presets (the firmware's preset LEDs on GPB0-4 stay unconnected). Footswitch wire pads 12 mm beside each M12
  hole; RJ45 25 mm right of centre, address jumpers next to the MCP.
- Main hangs on its nine encoders (layout in [README.md](README.md#layout)); its indicators are SMD LEDs only.
  The flavor toggle's body goes through an 18 mm hole, its wires on 3 pads below it. Bottom side, **soldered by
  hand** (holes only, not in the BOM): the Feather's two female headers (USB-A end towards the top edge) and
  6 RJ45 with their openings flush with the bottom edge. Main 1 sits left of AMP, Main 2 on the top side between
  the Feather's socket rows; bus pull-ups (2.2k SCL / SDA, 10k INT) below the Feather.

## JLCPCB parts (all checked in the JLCPCB library, 2026-10-20)

| Part | LCSC | Class |
|---|---|---|
| MCP23017-E/SS | C506653 | extended |
| SRV05-4 (TVS) | C558418 | extended |
| PEC11R-4015F-S0024 (THT) | C143789 | extended |
| R-RJ45R08P-A004 (THT) | C385834 | extended |
| 10k / 470R 0603, 4×10k array (encoder filters), 10n / 100n 0603, 10u 0805, red LED 0603 | C25804 / C23179, C29718, C57112 / C14663, C15850, C2286 | basic |
| Main: FJ5161BH 0.56" digit, red, common anode (THT) | C8092 | extended |
| Main: blue / orange LED 0603 (XL-1608UBC-04 / XL-1608UOC-06) | C965807 / C965800 | extended |
| Main: 74HC595D, 2.2k / 100R 0603, green LED 0805 (KT-0805G) | C5947, C4190 / C22775, C2297 | basic |
| Main, hand-soldered (not in the BOM): female headers 1×16 / 1×12 (e.g. KH-2.54FH-1X16P-H8.5 / -1X12P), R-RJ45R08P-A004 × 6 | (C2905421 / C2905419, C385834) | — |

Rough cost per design (Economic PCBA, as published by JLCPCB): setup ~$8 + stencil ~$1.5 + 4 extended × $3
(3 on Presets) + hand soldering $3.5 + joints, plus parts per board (encoders ~$2.7 each at 1+, ~$1.7 at 75+;
MCP ~$1.1) and the bare boards. Effect / Echo ≈ $25 per order + ≈ $15.5 per board. The address jumpers are
copper on the board: no part, no cost. Main: 6 extended part numbers (MCP23017, SRV05-4, encoder, FJ5161BH,
blue and orange LED) ≈ $18 in fees; THT on the top side only (9 encoders, 2 digits, ~83 joints).

## Not verified / open

- Encoder land pattern: corrected in `PedalLib` to the Bourns PEC11R datasheet on 2026-11-21 (pin rows 7.5 / 7.0 mm,
  lugs ±6.6 mm, body 6.5 mm, not 6). Only Gate + Comp and Effect are regenerated with it so far; the others fail the
  library check (`Checks.test_drc`) until each is regenerated.
- RJ45 pin numbering = T568B wire order in README.md; check against the existing cables before ordering.
- Lamp / LED / footswitch positions on the panel follow the board layout; no drill template yet.
- Gerbers are not exported yet (zones are filled by KiCad on export).
- Economic PCBA places parts on one side only, so the pedals' RJ45 (bottom side, with THT encoders on top) may
  also have to be hand-soldered: check in the JLC quote.
- Main: the Feather header pinout (JP1 / JP3) comes from the Adafruit board files; check it against the real
  Feather before soldering the sockets. The toggle hole (18 mm) assumes a mini toggle; check its body size.
- INT: every MCP drives the shared INT line push-pull by default; the firmware must set `IOCON.ODR=1` before it
  uses INT.
- `routing.pkl` (routing cache, ~30 KB each) — tracked or ignored is your call; without it a board reroutes in
  1–30 min and comes out the same.
