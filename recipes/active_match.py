#!/usr/bin/env python3
"""
Active Match Measurement Recipe

Demonstrates proper vna_library architecture:
- Uses models for configuration (SweepConfig, TraceSpec, etc.)
- Programs against the instrument-agnostic VNA interface; connect()
  picks the driver (E5071C or PNA family) from *IDN?
- Orchestrates measurement workflow at recipe level

Measurement: Active Match = sqrt(|S11|^2 + |S21|^2)
Frequency Bands: L2 (1.2156-1.2396 GHz), L1 (1.56342-1.58742 GHz)

Primary focus: Active match equation with marker searches
Secondary (optional): S11 limit envelope for return loss verification
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

# Allow running as a standalone script (src/ layout, no install step).
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (_REPO_ROOT / "src", _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from vna_library import (  # noqa: E402
    VNA,
    connect,
    SweepConfig,
    TraceSpec,
    EquationSpec,
    MarkerSpec,
    MarkerFunc,
)
from presets.limits import multiband_return_loss  # noqa: E402


# ---------------------------------------------------------------------------
# Band plan
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Band:
    name: str
    f_start_hz: float
    f_stop_hz: float


L2 = Band("L2", 1.2156e9, 1.2396e9)
L1 = Band("L1", 1.56342e9, 1.58742e9)
BANDS = (L2, L1)

# A single sweep wide enough to cover both bands, with guard margins.
SWEEP = SweepConfig(
    start_hz=1.20e9,
    stop_hz=1.60e9,
    points=1601,
    if_bw_hz=1e3,
    power_dbm=0.0,
    avg_count=1,
)

# Trace / equation names as they appear on the instrument.
S11 = TraceSpec(name="Tr1", sparam="S11", fmt="MLOG")
S21 = TraceSpec(name="Tr2", sparam="S21", fmt="MLOG")
ACTIVE_MATCH = EquationSpec(
    name="AMatch",
    # Linear active match magnitude, displayed in log-mag.
    expression="sqrt(abs(S11)^2+abs(S21)^2)",
)

# Return loss must stay at or below this across both bands.
RETURN_LOSS_MAX_DB = -10.0


@dataclass(frozen=True)
class BandResult:
    band: str
    peak_freq_hz: float
    peak_value: float  # active match, log-mag (dB)


def measure_active_match(
    resource: str,
    *,
    apply_s11_limits: bool = True,
    ch: int = 1,
) -> Dict[str, BandResult]:
    """Configure the VNA, sweep, and read the worst-case active match per band.

    Instrument-agnostic: the driver is picked from *IDN? via connect(),
    so this runs unchanged on an E5071C or a PNA-family instrument
    (E5080A/B, ENA-X, N52xx, P50xx, M980x).  Note the equation-editor
    expression syntax differs between families; ACTIVE_MATCH is written
    for the E5071C editor and may need adjusting for the PNA editor.

    Workflow:
      1. Preset and apply the wideband sweep.
      2. Define S11, S21 traces and the active-match equation trace.
      3. (Optional) Load an S11 return-loss envelope across both bands.
      4. Trigger a single sweep.
      5. For each band, place a MAX marker on the equation trace and read
         the worst-case active match.

    Args:
        resource: VISA resource string (e.g. "TCPIP0::192.168.0.10::inst0::INSTR").
        apply_s11_limits: If True, load the return-loss limit envelope on S11.
        ch: Instrument channel to use.

    Returns:
        Mapping of band name -> :class:`BandResult`.
    """
    vna: VNA = connect(resource)
    vna.preset()
    vna.apply_sweep(SWEEP, ch=ch)

    # Traces: S11 and S21 feed the equation; active match is its own trace.
    vna.clear_traces(ch=ch)
    vna.define_trace(S11, ch=ch, window=1, trace_num=1)
    vna.define_trace(S21, ch=ch, window=1, trace_num=2)
    vna.define_equation(ACTIVE_MATCH, ch=ch, window=1, trace_num=3)

    # Optional S11 return-loss verification across both bands.
    if apply_s11_limits:
        table = multiband_return_loss(
            [(b.f_start_hz, b.f_stop_hz) for b in BANDS],
            max_db=RETURN_LOSS_MAX_DB,
        )
        vna.select_trace(S11.name, ch=ch)
        vna.apply_limit_table_bulk(table, ch=ch, meas_name=S11.name)

    # Acquire.
    vna.single_sweep(ch=ch)

    # Search each band on the active-match trace with a dedicated marker.
    vna.select_trace(ACTIVE_MATCH.name, ch=ch)
    results: Dict[str, BandResult] = {}
    for i, band in enumerate(BANDS, start=1):
        # Domain is per-channel; clear before retargeting to the next band.
        vna.marker_clear_domain(ch=ch)
        spec = MarkerSpec(
            index=i,
            f_start_hz=band.f_start_hz,
            f_stop_hz=band.f_stop_hz,
            func=MarkerFunc.MAX,
            enable_domain=True,
        )
        vna.marker_configure(spec, ch=ch)
        vna.opc()
        x, y = vna.marker_read(i, ch=ch)
        results[band.name] = BandResult(band.name, x, y)

    return results


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="E5071C active-match measurement")
    parser.add_argument(
        "resource",
        help="VISA resource, e.g. TCPIP0::192.168.0.10::inst0::INSTR",
    )
    parser.add_argument(
        "--no-limits",
        action="store_true",
        help="Skip the S11 return-loss limit envelope",
    )
    parser.add_argument("--channel", type=int, default=1, help="Instrument channel")
    args = parser.parse_args(argv)

    results = measure_active_match(
        args.resource,
        apply_s11_limits=not args.no_limits,
        ch=args.channel,
    )

    print(f"Active match (worst case per band), return-loss max {RETURN_LOSS_MAX_DB:g} dB:")
    for name, r in results.items():
        print(f"  {name}: {r.peak_value:8.3f} dB @ {r.peak_freq_hz / 1e9:.6f} GHz")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
