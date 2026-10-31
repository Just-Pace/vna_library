# vna_library

Python control library for Keysight vector network analyzers, built on SCPI
over VISA (pyvisa). Two drivers share one transport layer:

- **`E5071C`** — the ENA dialect (E5071C benchtop).
- **`PNA`** — the PNA / new-platform dialect: E5080A/B ENA-X, M980xA/M983xA
  PXIe VNAs, and P937xA / P50xxA Streamline USB VNAs.

## Install

```bash
pip install -e ".[dev]"
```

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
| Formatted data | two values per point, always | one per point (two for Polar/Smith) |
| Single sweep | `TRIG:SOUR BUS` + `TRIG:SING` | `TRIG:SOUR IMM` + `SENS:SWE:MODE SING` |
| Marker search bands | one shared domain per channel | 16 independent user ranges |
| Equation traces | equation is its own trace type | equation attaches to an existing measurement |

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
