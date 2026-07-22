# vna_library

Python control library for the Keysight E5071C ENA vector network analyzer,
built on SCPI over VISA (pyvisa).

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
