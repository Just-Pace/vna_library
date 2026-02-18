#!/usr/bin/env python3
"""
Active Match Measurement Recipe

Demonstrates proper vna_library architecture:
- Uses models for configuration (SweepConfig, TraceSpec, etc.)
- Uses E5071C driver for hardware control
- Orchestrates measurement workflow at recipe level

Measurement: Active Match = sqrt(|S11|^2 + |S21|^2)
Frequency Bands: L2 (1.2156-1.2396 GHz), L1 (1.56342-1.58742 GHz)

Primary focus: Active match equation with marker searches
Secondary (optional): S11 limit envelope for return loss verification
"""
import argparse
import csv
import time
from typing import Dict, List, Tuple, Optional
from sys import path
from pathlib import Path

# Add parent directory to path to enable relative imports
path.insert(0, str(Path(__file__).parent.parent))

from vna_library import (
    E5071C, VNAError, InstrumentError,
    SweepConfig, TraceSpec, EquationSpec,
    LimitTable, LimitSegment, LimitType,
    MarkerSpec, MarkerFunc
)


# Sweep Configuration
SWEEP_START_HZ = 1.1e9
SWEEP_STOP_HZ = 1.7e9
SWEEP_POINTS = 601
IF_BANDWIDTH_HZ = 1e3
POWER_DBM = 0.0
AVG_COUNT = 4

# Marker Search Bands
L2_START_HZ = 1.2156e9
L2_STOP_HZ = 1.2396e9
L1_START_HZ = 1.56342e9
L1_STOP_HZ = 1.58742e9

# Limit Envelope Data (f_start, f_stop, y_start, y_stop)
# Note: Optional feature for S11 return loss checking
LIMIT_DATA = [
    (1.2156e9,  1.2256e9,  -3,    -7),
    (1.2256e9,  1.2296e9,  -7,    -7),
    (1.2296e9,  1.2396e9,  -7,    -3),
    (1.2156e9,  1.2256e9,  -2.5,  -6),
    (1.2256e9,  1.2296e9,  -6,    -6),
    (1.2296e9,  1.2396e9,  -6,    -2.5),
    (1.56342e9, 1.57342e9, -5,    -7),
    (1.57342e9, 1.57742e9, -7,    -7),
    (1.57742e9, 1.58742e9, -7,    -5),
]


def build_limit_table() -> LimitTable:
    """Build limit table from LIMIT_DATA configuration."""
    segments = [
        LimitSegment(
            kind=LimitType.MAX,
            f_start_hz=fs,
            f_stop_hz=fe,
            y_start_db=y_start,
            y_stop_db=y_stop
        )
        for fs, fe, y_start, y_stop in LIMIT_DATA
    ]
    return LimitTable.from_iter(segments)


def save_csv(data: List[Tuple[float, float]], filename: str) -> None:
    """Export frequency/magnitude data to CSV file."""
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Frequency_Hz", "Magnitude_dB"])
        for freq, mag in data:
            writer.writerow([freq, mag])
    print(f"[OK] Data saved to {filename}")


def run_active_match(
    gpib_address: str = "GPIB0::16::INSTR",
    csv_output: str = "active_match_results.csv",
    state_file: Optional[str] = None,
    apply_s11_limits: bool = False
) -> Dict[str, Tuple[float, float]]:
    """
    Execute active match measurement recipe.

    Primary measurement: Active match (EQ1) with marker searches in L2/L1 bands.
    Secondary check (optional): S11 limit envelope for return loss verification.

    Args:
        gpib_address: GPIB resource string for VNA
        csv_output: Output CSV filename for active match data
        state_file: Optional VNA state file path to save
        apply_s11_limits: If True, apply limit envelope to S11 trace (secondary check)

    Returns:
        Dictionary with marker results: {"L2": (freq_hz, mag_db), "L1": (freq_hz, mag_db)}

    Raises:
        VNAError: On communication errors
        InstrumentError: On instrument-reported errors
    """
    try:
        # Connect and reset
        print(f"[INFO] Connecting to VNA at {gpib_address}...")
        vna = E5071C(gpib_address, timeout_ms=30000)
        print(f"[INFO] Connected to: {vna.idn()}")

        print("[INFO] Resetting VNA...")
        vna.reset()
        vna.preset()

        # Configure sweep
        print("[INFO] Configuring sweep parameters...")
        sweep = SweepConfig(
            start_hz=SWEEP_START_HZ,
            stop_hz=SWEEP_STOP_HZ,
            points=SWEEP_POINTS,
            if_bw_hz=IF_BANDWIDTH_HZ,
            power_dbm=POWER_DBM,
            avg_count=AVG_COUNT
        )
        vna.apply_sweep(sweep, ch=1)

        # Define traces
        print("[INFO] Defining S-parameter traces...")
        vna.clear_traces(ch=1)

        tr1 = TraceSpec(name="Tr1", sparam="S11", fmt="MLOG")
        vna.define_trace(tr1, ch=1, window=1, trace_num=1)

        tr2 = TraceSpec(name="Tr2", sparam="S21", fmt="MLOG")
        vna.define_trace(tr2, ch=1, window=1, trace_num=2)

        # Define equation trace - CRITICAL: Use * not ^ operator
        print("[INFO] Defining active match equation...")
        eq1 = EquationSpec(
            name="EQ1",
            expression="Sqrt( Mag(S11)*Mag(S11) + Mag(S21)*Mag(S21) )"
        )
        vna.define_equation(eq1, ch=1, window=1, trace_num=3)

        # Apply limit table (optional secondary check)
        if apply_s11_limits:
            print("[INFO] Applying S11 limit envelope (secondary check)...")
            vna.select_trace("Tr1", ch=1)
            limits = build_limit_table()
            vna.apply_limit_table_bulk(
                limits,
                ch=1,
                meas_name="Tr1",
                clear_first=True,
                enable_display=True,
                enable_test=True,
                check_errors=True
            )
            print("[INFO] S11 limit envelope applied")

        # Configure markers - CRITICAL: Select EQ1 first (markers bind to active trace)
        print("[INFO] Configuring markers on active match equation...")
        vna.select_trace("EQ1", ch=1)

        # Marker 1 - L2 band
        marker1 = MarkerSpec(
            index=1,
            f_start_hz=L2_START_HZ,
            f_stop_hz=L2_STOP_HZ,
            func=MarkerFunc.MIN,
            enable_domain=True
        )
        vna.marker_configure(marker1, ch=1)

        # CRITICAL: Clear domain before setting new range (per-channel constraint)
        vna.marker_clear_domain(ch=1)

        # Marker 2 - L1 band
        marker2 = MarkerSpec(
            index=2,
            f_start_hz=L1_START_HZ,
            f_stop_hz=L1_STOP_HZ,
            func=MarkerFunc.MIN,
            enable_domain=True
        )
        vna.marker_configure(marker2, ch=1)

        # Sync after marker configuration
        vna.opc()

        # Optional state save
        if state_file:
            print(f"[INFO] Saving VNA state to {state_file}...")
            vna.write(f'MMEM:STOR:STAT "{state_file}"')
            time.sleep(1)
            print("[OK] State saved")

        # Trigger measurement
        print("[INFO] Triggering single sweep...")
        vna.single_sweep(ch=1)

        # Fetch data
        print("[INFO] Fetching measurement data...")
        data = vna.fetch_fdata_pairs(ch=1)
        save_csv(data, csv_output)

        # Read markers
        print("[INFO] Reading marker results...")
        l2_freq, l2_mag = vna.marker_read(1, ch=1)
        l1_freq, l1_mag = vna.marker_read(2, ch=1)

        print(f"[OK] L2 Band Marker 1: {l2_freq:.6e} Hz → {l2_mag:.2f} dB")
        print(f"[OK] L1 Band Marker 2: {l1_freq:.6e} Hz → {l1_mag:.2f} dB")

        return {
            "L2": (l2_freq, l2_mag),
            "L1": (l1_freq, l1_mag)
        }

    except InstrumentError as e:
        print(f"[ERROR] Instrument error: {e}")
        raise
    except VNAError as e:
        print(f"[ERROR] VNA communication error: {e}")
        raise
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}")
        raise


def main():
    """CLI entry point for active match recipe."""
    parser = argparse.ArgumentParser(
        description="Active Match measurement recipe for E5071C VNA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic active match measurement with markers
  python active_match.py --gpib "GPIB0::16::INSTR" --csv-output results.csv

  # Include S11 limit envelope check
  python active_match.py --apply-s11-limits
        """
    )
    parser.add_argument(
        "--gpib",
        default="GPIB0::16::INSTR",
        help="GPIB resource address (default: GPIB0::16::INSTR)"
    )
    parser.add_argument(
        "--csv-output",
        default="active_match_results.csv",
        help="Output CSV filename (default: active_match_results.csv)"
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="Optional VNA state file path to save"
    )
    parser.add_argument(
        "--apply-s11-limits",
        action="store_true",
        help="Apply limit envelope to S11 trace (secondary check, default: off)"
    )

    args = parser.parse_args()

    try:
        results = run_active_match(
            gpib_address=args.gpib,
            csv_output=args.csv_output,
            state_file=args.state_file,
            apply_s11_limits=args.apply_s11_limits
        )
        print("\n[RESULTS]")
        print(f"  L2 Band Marker 1: {results['L2'][0]:.3e} Hz → {results['L2'][1]:.2f} dB")
        print(f"  L1 Band Marker 2: {results['L1'][0]:.3e} Hz → {results['L1'][1]:.2f} dB")
    except (VNAError, InstrumentError) as e:
        print(f"[ERROR] Measurement failed: {e}")
        return 1
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
