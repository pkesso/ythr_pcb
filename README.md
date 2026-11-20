# PCBs — THR pedalboard

Hardware spec for the JLCPCB build of the control surface. The prototype it
replaces is described in the firmware repository (`feather/doc/wiring.md`); pin
assignments and the 7-segment bit order stay as there, so the firmware does not
change.

## Setup

KiCad 10 (`kicad-cli`) and Python 3 with the packages in `requirements.txt`:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python effect/gen_effect.py                      # one board
.venv/Scripts/python -m unittest discover -s tests -t tests    # all tests
```

How the boards are generated and tested: [common/README.md](common/README.md).

## Boards

| Dir | Board | MCP23017 | Size (max) | Mask colour |
|-----|-------|----------|------------|-------------|
| `main/` | Main: Main 1 + Main 2 + 7-seg + Feather + bus hub | 0x20, 0x21 | 180 × 110 mm | green |
| `gain-compressor/` | Gate + Compressor (Rev0: hot-swap I²C buffer, VCC ferrite, debounced footswitches) | 0x22 | 60 × 116 mm (Hammond 1590N1 template) | yellow |
| `effect/` | Effect | 0x23 | 50 × 65 mm | purple |
| `echo/` | Echo | 0x24 | 50 × 65 mm | white (black silk) |
| `rever/` | Reverb | 0x25 | 50 × 65 mm | blue |
| `presets/` | Presets | 0x26 | 200 × 50 mm | black |

Develop (0x27) gets no PCB.

`common/` is not a board: it holds the generator scripts and the shared KiCad libraries
(see [common/README.md](common/README.md)); `tools/` holds the KiCad helper scripts.

## Mechanics

- Panel: 2 mm. The board hangs on the EC11 bushings (panel rests on the bushing
  shoulder, held by the M7 nuts) — no standoffs.
- Board-to-panel gap = EC11 body height = **6 mm**. Anything taller than 6 mm
  goes on the **bottom side** (Feather, RJ45) or through the panel (the
  7-segment digits, 8 mm: their faces reach the panel window).
- Footswitches and bezel lamps mount to the panel only and are wired to the
  board; they do not overlap the board.
- Main: the Feather's USB-A towards the **top** wall, the 6× RJ45 on the
  **bottom** wall. No USB-C on the board (the Feather headers carry no USB data):
  power and flashing through the Feather's own USB-C.
- Pedal "front" = the far (top) edge, away from the foot: RJ45 there.

## Layout

- **Pedals** (50 × 65, top = RJ45 edge): 4 encoders in a square; with 5, the
  5th in the center. SMD LEDs in a row along the bottom edge, ~10 mm below
  the lower encoders. Bezel lamps and footswitches sit in the panel just below
  the board edge (not over the board).
- **Main** (180 × 110, centre line x = 90): 5 encoders in the lower row (Gain,
  Master, Bass, Mid, Treble) on a 30 mm pitch, 25 mm above the bottom edge, the
  middle one on the centre line; 4 in the upper row, 25 mm higher, offset by
  15 mm (Amp, Cab, Guitar, Audio). 7-seg top centre (y = 25). Flavor toggle top
  left (40, 35) with its 3 LEDs on a 25 mm radius: blue (Boutique) straight
  above, green (Modern) and orange (Classic) 30° to the left / right. 4 battery
  LEDs in a row top right (x = 140…170, y = 22). Feather (bottom side) right of
  the display, 6 RJ45 along the bottom edge, 3 on each side of the centre.

## MCP23017 block (per chip)

Based on Adafruit #5346.

- MCP23017-E/SS, **SSOP-28** (LCSC C506653).
- 0.1 µF at VDD.
- Address: three **3-pad solder jumpers** (SolderJumper-3 Bridged12), one per bit, pad 2 = Ax. Pad 1 is
  bridged at the factory and goes to this board's level of the bit, pad 3 to the other rail: to change a bit,
  cut the 1–2 bridge and solder 2–3. On every board the left pad is GND (0) and the right pad VCC (1):
  a jumper for a 1 is placed rotated by 180°, so the factory bridge sits on the side of the bit's value.
  `0 1` is printed above the column, `A0`–`A2` beside each jumper. No pull-downs needed.
- 10k pull-up on RESET.
- INTA + INTB wired together (firmware: `IOCON.MIRROR=1, ODR=1`) → bus INT line.
- No power LED.
- I²C pull-ups 2×10k behind a solder jumper, **open by default** (the bus
  pull-ups live on Main).

## Encoder (EC11)

- EC11 with push switch (Bourns PEC11R-4015F-S0024), **15 mm D-shaft**, on the board. The push switch is
  not connected, except on Echo: pushing TIME is a second TAP (switch in parallel with the TAP button).
- Pinout A–C–B (C = common → GND); mounting lugs → GND.
- Per channel (A and B): 10k pull-up to 3.3 V → 10k series → 10 nF to GND at
  the MCP pin (prototype encoder module circuit).

## Footswitch (PBS-24, 12 mm)

- Panel-mounted, wired. On the board: 2 solder holes (GPIO, GND), 2.54 mm
  pitch, ~1.0 mm drill, silkscreen label, near the board edge.
- No filter — direct to the MCP GPIO (internal pull-up + firmware debounce),
  as in the prototype.
- Same 2-hole wiring for the small Echo **tap** button.

## Flavor toggle (Main)

- Momentary toggle **(ON)-OFF-(ON)**, panel-mounted, wired: 3 solder holes
  (GPA0 = prev, GND, GPB0 = next) below an **18 mm hole** in the board for the
  toggle's body (it does not fit in the 6 mm gap).

## Indicator

- External bezel lamp (6 mm panel hole, "3–6 V", built-in resistor): 2 solder
  holes (`+` = GPIO, `−` = GND), no resistor on the board.
- In parallel: **SMD LED 0603, red (JLC basic)** + 470 Ω, in series with a
  solder jumper **open by default** (close it for bench use / clear enclosure).
- Main: **SMD LEDs only** (no lamp pads, no jumper), resistor straight from the
  MCP pin: flavor blue 0603 (extended) and green 0805 (basic) with 100 Ω (higher
  forward voltage), orange 0603 (extended) with 470 Ω; battery LEDs red 0603 +
  470 Ω. Names printed above the LEDs.

## 7-segment display (Main, via Main 2 = 0x21)

- Two single-digit **0.56"** displays (digit body 12.6 × 19 mm), **common
  anode**, **red FJ5161BH** (LCSC C8092, THT, 8 mm tall: the face reaches the
  panel window). Orange / yellow ones with this pinout are not stocked by JLC;
  any 5161BS-type digit fits the same footprint (hand-soldered, then without
  the LCSC number).
- 2 × 74HC595D (basic, one per digit, static drive) + 16 × 470 Ω.
- Signals: GPB6 = SDI, GPA6 = SCLK, GPB7 = LOAD. Bit order as in the
  prototype (WHMXE-595-2): 16-bit word, MSB first; low byte = left digit,
  bit0 = a … bit7 = dp; segment lit = 0.

## Main board

- 2 × MCP23017 (0x20 Main 1, 0x21 Main 2), 9 × EC11, flavor toggle,
  7 indicators, 7-seg.
- **Feather RP2040 USB Host (#5723)** on female headers (1×16 + 1×12), bottom
  side, USB-A towards the top wall. JP1.13 GND, JP1.14–15 3V3, JP3.11 SCL,
  JP3.12 SDA, JP3.10 (D5) INT; nothing else connected.
- **6 × RJ45** (5 pedals + 1 spare), star topology, bottom side, plug openings
  flush with the bottom edge. TVS (SRV05-4) on SCL / SDA / INT / VCC at each jack.
- Feather sockets and RJ45 are **soldered by hand** (holes only, not in the
  BOM): JLC Economic PCBA places parts on one side only.
- Bus pull-ups (the only ones on the bus): SCL / SDA 2.2k, INT 10k → 3.3 V;
  INT → Feather D5 (not used by the firmware yet; it will need `IOCON.ODR=1`
  on every chip, since all INT outputs share the line).
- Main 1 left of the Amp encoder, Main 2 on the top side between the Feather's
  socket rows (the Feather is underneath).
- Bus power: 3.3 V from the Feather regulator.
- Bluetooth: external CSR8510 dongle (not integrated).

## Pedal boards

- 1 × MCP23017 + controls (see the per-pedal tables in wiring.md) + 1 × RJ45
  (bottom side, front edge) + 10 µF bulk at the jack.

## Bus cable (8P8C)

| Wire | Signal |
|------|--------|
| White-Orange | GND |
| Orange | SCL |
| White-Green | GND |
| Blue | SDA |
| White-Blue | GND |
| Green | VCC 3.3 V |
| White-Brown | GND (return for INT) |
| Brown | **INT** (open-drain, active low) |

## Parts / cost notes

- Prefer JLC **basic** parts; each **extended** part costs +$3 per design.
- SMD LED: red, basic — no extended fee.
- Final LCSC part numbers and prices — at the BOM stage.
