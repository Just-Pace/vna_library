# vna_library

Python control library for Keysight vector network analyzers, built on SCPI
over VISA (pyvisa). Two drivers share one transport layer:

- **`E5071C`** — the ENA dialect (E5071C benchtop).
- **`PNA`** — the PNA / new-platform dialect: E5080A/B ENA-X, M980xA/M983xA
  PXIe VNAs, and P937xA / P50xxA Streamline USB VNAs.

## Install

Requires Python 3.10+. pyvisa alone cannot talk to instruments — it needs a
**backend**, and that is the piece a fresh machine is missing when you see
`Could not locate a VISA implementation`.

```bash
# Library + tests (tests need no hardware and no backend):
pip install -e ".[dev]"

# Plus ONE backend to open real instruments — either:
pip install -e ".[backend-py]"   # pyvisa-py: pure Python, no admin rights.
                                 # TCPIP/HiSLIP — enough for P50xx/P93xx USB
                                 # VNAs, which are reached via HiSLIP to the
                                 # firmware running on the host PC.
# ...or install a vendor library (adds direct USB/GPIB):
#   Windows / Linux / macOS: Keysight IO Libraries Suite
#     https://www.keysight.com/find/iosuite
#   (NI-VISA also works.)
```

The driver tries the vendor backend first and falls back to pyvisa-py
automatically; force one with `PNA.open(resource, backend="@py")`. If neither
is present, `open()` raises an error telling you exactly this.

### Bench verification

```bash
python recipes/bench_check_pna.py TCPIP0::localhost::hislip0::INSTR
```

runs a non-destructive check sequence (measurement creation, independent
limit tables, sweep, data lengths, marker band isolation, channel-scoped
clearing) and prints PASS/FAIL per item. Run it once per instrument model /
firmware before trusting the driver unattended.

## Usage

```python
from vna_library import E5071C, SweepConfig, TraceSpec, LimitTable, LimitSegment, LimitType

with E5071C.open("TCPIP0::192.168.0.10::inst0::INSTR") as vna:
    print(vna.idn())

    vna.apply_sweep(SweepConfig(start_hz=1.0e9, stop_hz=2.0e9, points=401))
    vna.define_trace(TraceSpec(name="Tr1", sparam="S11"))

    table = LimitTable.from_iter([
        LimitSegment(LimitType.MAX, 1.2e9, 1.3e9, -10.0, -10.0),
    ])
    vna.apply_limit_table_bulk(table, meas_name="Tr1")

    vna.single_sweep()
    freqs = vna.fetch_frequencies()
    data = vna.fetch_fdata_pairs()
```

## Usage (PNA family: P50xx / P93xx / M98xx / E5080x)

Everything is addressed by explicit measurement number — no selected-trace
state, so commands can't land on the wrong trace:

```python
from vna_library import PNA, SweepConfig, TraceSpec, LimitTable, LimitSegment, LimitType

with PNA.open("TCPIP0::192.168.0.20::hislip0::INSTR") as vna:
    vna.apply_sweep(SweepConfig(start_hz=1.0e9, stop_hz=2.0e9, points=401))
    vna.define_trace(TraceSpec(name="s11", sparam="S11"), meas=1)
    vna.define_trace(TraceSpec(name="s21", sparam="S21"), meas=2)

    table = LimitTable.from_iter([
        LimitSegment(LimitType.MAX, 1.2e9, 1.3e9, -10.0, -10.0),
    ])
    vna.apply_limit_table_bulk(table, meas=1)   # lands on meas 1, deterministically

    vna.single_sweep()
    freqs = vna.fetch_frequencies(meas=1)
    data = vna.fetch_fdata(meas=1)              # ONE value per point (scalar formats)
```

## Dialect differences that will bite you

| | E5071C (ENA) | PNA family |
|---|---|---|
| Trace addressing | selected trace (`CALC:PAR:SEL "name"`) | measurement number (`CALC:MEAS<n>`) |
| Limit table write | leading segment count required | **no** count prefix |
| Clear limit table | `CALC:LIM:DATA 0` (no DELete cmd) | `CALC:MEAS<n>:LIM:DATA:DEL` |
| Clear traces | `CALC:PAR:COUN 1` (no PAR:DEL:ALL) | per-meas `CALC:MEAS<n>:DEL` (`PAR:DEL:ALL` is **instrument-wide**) |
| Formatted data | two values per point, always | one per point (two for Polar/Smith) |
| Single sweep | `TRIG:SOUR BUS` + `TRIG:SING` | `TRIG:SOUR IMM` + `SENS:SWE:MODE SING` |
| Marker search bands | one shared domain per channel | 16 user ranges per **channel** (driver allocates per measurement+marker) |
| Equation traces | `EQU:TEXT` on an existing trace | `EQU:TEXT` on an existing measurement |

## Limit lines: things the instrument won't tell you

- **Limit tables are per-trace.** `:CALC{ch}:LIM:DATA` writes to the currently
  selected trace (`CALC:PAR:SEL`). To put limits on several traces, pass
  `meas_name` for each call — otherwise the second table silently overwrites
  the first trace's.
- **There is no `CALC:LIM:DEL:ALL` on the E5071C.** That is PNA-family syntax
  and produces an `-113,"Undefined header"` error here. Clearing is done by
  writing a zero-segment table (`:CALC{ch}:LIM:DATA 0`), which is what
  `limits_clear()` does.

## Testing

The driver accepts any object implementing the small `VisaSession` protocol,
so the full SCPI logic is unit-testable without hardware or pyvisa:

```bash
pytest
```
