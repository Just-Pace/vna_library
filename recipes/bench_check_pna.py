#!/usr/bin/env python3
"""Bench verification for the PNA driver (P50xx / P93xx / M98xx / E5080x).

Run this once per instrument/firmware to confirm the driver's SCPI matches
reality. It is non-destructive apart from presetting the instrument and
creating measurements on channels 1 and 2.

Usage:
    python recipes/bench_check_pna.py TCPIP0::localhost::hislip0::INSTR
"""
from __future__ import annotations

import sys

from vna_library import (
    PNA,
    LimitSegment,
    LimitTable,
    LimitType,
    MarkerFunc,
    MarkerSpec,
    SweepConfig,
    TraceSpec,
)

CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    CHECKS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def main(resource: str) -> int:
    with PNA.open(resource) as vna:
        print(f"Connected: {vna.idn()}")
        vna.preset()
        vna.opc()

        sweep = SweepConfig(start_hz=1.0e9, stop_hz=2.0e9, points=201, if_bw_hz=1e3)
        vna.apply_sweep(sweep)
        vna.define_trace(TraceSpec(name="s11", sparam="S11"), meas=1)
        vna.define_trace(TraceSpec(name="s21", sparam="S21"), meas=2)
        check("define two measurements (no errors queued)", not vna.get_errors())

        # Independent limit tables per measurement
        vna.apply_limit_table_bulk(
            LimitTable.from_iter([LimitSegment(LimitType.MAX, 1.2e9, 1.4e9, -10.0, -10.0)]),
            meas=1,
        )
        vna.apply_limit_table_bulk(
            LimitTable.from_iter([LimitSegment(LimitType.MIN, 1.6e9, 1.8e9, -60.0, -60.0)]),
            meas=2,
        )
        t1 = vna.query_limit_table(meas=1)
        t2 = vna.query_limit_table(meas=2)
        check("limit table 1 lands on meas 1", len(t1) == 1 and t1[0][0] == 1)
        check("limit table 2 lands on meas 2", len(t2) == 1 and t2[0][0] == 2)
        check("tables are independent", t1 != t2)

        # Sweep + data
        vna.single_sweep()
        freqs = vna.fetch_frequencies(meas=1)
        data = vna.fetch_fdata(meas=1)
        check("sweep completed (no errors queued)", not vna.get_errors())
        check(f"frequency axis has {sweep.points} points", len(freqs) == sweep.points)
        check(f"formatted data has {sweep.points} points (1/pt)", len(data) == sweep.points)
        check("limit judgment returns a verdict", vna.limit_fail(meas=1) in (True, False))

        # Markers: same index on two measurements must keep separate bands
        vna.marker_configure(
            MarkerSpec(index=1, f_start_hz=1.2e9, f_stop_hz=1.3e9, func=MarkerFunc.MIN), meas=1
        )
        vna.marker_configure(
            MarkerSpec(index=1, f_start_hz=1.7e9, f_stop_hz=1.8e9, func=MarkerFunc.MIN), meas=2
        )
        vna.marker_search(1, MarkerFunc.MIN, meas=1)
        vna.opc()
        x1, _ = vna.marker_read(1, meas=1)
        x2, _ = vna.marker_read(1, meas=2)
        check("meas-1 marker stays in its band after meas-2 config", 1.2e9 <= x1 <= 1.3e9)
        check("meas-2 marker searches its own band", 1.7e9 <= x2 <= 1.8e9)

        # Channel-scoped clearing
        vna.define_trace(TraceSpec(name="x", sparam="S11"), ch=2, meas=5)
        vna.clear_traces(ch=1)
        remaining = vna.query("SYST:MEAS:CAT?").strip().strip('"')
        check("clear_traces(1) left channel 2 alone", remaining == "5")
        check("no errors queued at end", not vna.get_errors())

    failed = [n for n, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
