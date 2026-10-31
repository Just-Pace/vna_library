# src/vna_library/e5071c.py
from __future__ import annotations

import math
from typing import List, Optional, Tuple

from .base import (
    InstrumentError,
    ScpiInstrument,
    VisaSession,
    VNAError,
    VNATimeout,
    format_limit_payload,
)
from .models import SweepConfig, TraceSpec, EquationSpec, LimitTable, MarkerSpec, MarkerFunc, LimitType

__all__ = ["E5071C", "VNAError", "InstrumentError", "VNATimeout", "VisaSession", "LimitSeg"]

# (type, f_start_hz, f_stop_hz, y_start, y_stop)
LimitSeg = Tuple[int, float, float, float, float]


class E5071C(ScpiInstrument):
    """Driver for the Keysight E5071C ENA vector network analyzer.

    Accepts either a VISA resource string (opened via pyvisa) or any object
    implementing VisaSession — the latter lets tests run against a fake
    transport with no hardware or pyvisa installed.
    """

    def __init__(self, session: VisaSession | str, timeout_ms: int = 30000):
        super().__init__(session, timeout_ms)
        self._marker_domain: dict[int, tuple[float, float]] = {}  # ch → (start, stop)

    # basics
    def preset(self) -> None:
        self.write("SYST:PRES")
        self._marker_domain.clear()

    def reset(self) -> None:
        super().reset()
        self._marker_domain.clear()

    # configuration
    def apply_sweep(self, sweep: SweepConfig, ch: int = 1) -> None:
        self.write(f"SENS{ch}:FREQ:STAR {sweep.start_hz}")
        self.write(f"SENS{ch}:FREQ:STOP {sweep.stop_hz}")
        self.write(f"SENS{ch}:SWE:POIN {sweep.points}")
        self.write(f"SENS{ch}:BWID {sweep.if_bw_hz}")
        self.write(f"SENS{ch}:AVER:STAT {'ON' if sweep.avg_count > 1 else 'OFF'}")
        if sweep.avg_count > 1:
            self.write(f"SENS{ch}:AVER:COUN {sweep.avg_count}")
        self.write(f"SOUR{ch}:POW {sweep.power_dbm}")

    def clear_traces(self, ch: int = 1) -> None:
        self.write(f"CALC{ch}:PAR:DEL:ALL")

    def define_trace(self, trace: TraceSpec, ch: int = 1, window: int = 1, trace_num: int = 1) -> None:
        self.write(f'CALC{ch}:PAR:DEF "{trace.name}","{trace.sparam}"')
        self.write(f'DISP:WIND{window}:TRAC{trace_num}:FEED "{trace.name}"')
        self.write(f"CALC{ch}:FORM {trace.fmt}")

    def define_equation(self, eq: EquationSpec, ch: int = 1, window: int = 1, trace_num: int = 3) -> None:
        self.write(f'CALC{ch}:PAR:DEF "{eq.name}",EQU')
        self.write(f'CALC{ch}:PAR:SEL "{eq.name}"')
        self.write(f'CALC{ch}:EQU:DEF "{eq.expression}"')
        self.write(f'DISP:WIND{window}:TRAC{trace_num}:FEED "{eq.name}"')
        self.write(f"CALC{ch}:EQU:STAT ON")

    def select_trace(self, trace_name: str, ch: int = 1) -> None:
        self.write(f'CALC{ch}:PAR:SEL "{trace_name}"')

    # limits (Phase-1)
    #
    # E5071C limit tables are PER-TRACE: every limit command below acts on the
    # currently selected trace (CALC:PAR:SEL).  To put limits on several traces,
    # select each trace and apply its table separately.
    def limits_on(self, ch: int = 1) -> None:
        self.write(f"CALC{ch}:LIM:STAT ON")

    def limits_clear(self, ch: int = 1, meas_name: Optional[str] = None) -> None:
        """Clear the limit table of the selected trace.

        The E5071C has no CALC:LIM:DEL:ALL command (that is PNA-family
        syntax and produces an 'Undefined header' error here); the documented
        way to clear a table is writing a zero-segment table.

        Args:
            ch: Channel number.
            meas_name: Optional trace to select first.  If None, clears the
                currently selected trace's table.
        """
        if meas_name is not None:
            self.select_trace(meas_name, ch=ch)
        self.write(f"CALC{ch}:LIM:DATA 0")

    def apply_limit_table_bulk(
        self,
        table: LimitTable,  # Keep existing interface
        ch: int = 1,
        meas_name: Optional[str] = None,
        clear_first: bool = True,
        enable_display: bool = True,
        enable_test: bool = True,
        check_errors: bool = True,
) -> None:
        """
        Bulk-load a limit table via :CALC{ch}:LIM:DATA exactly as E5071C expects.

        The instrument expects:
          :CALC1:LIM:DATA <num>,<type1>,<fstart1>,<fstop1>,<ystart1>,<ystop1>,<type2>,...

        Where:
          type: 1=MAX, 2=MIN
          frequencies in Hz
          Y units match the currently selected format (dB, dBm, etc.)

        Limit tables are per-trace.  The table lands on the selected trace, so
        pass meas_name (or select_trace() yourself) for every trace you load;
        loading a second trace's table without re-selecting overwrites the
        first trace's table instead.

        Args:
            table: LimitTable containing segments to apply.
            ch: Channel number (default: 1).
            meas_name: Optional measurement/trace name (e.g., "TR2", "EQ1").
                      If None, uses currently selected trace (caller must ensure correct selection).
            clear_first: If True, clear the selected trace's table (zero-segment
                      write) before loading.  Mostly redundant since LIM:DATA
                      replaces the whole table, but kept as a belt-and-braces
                      reset of any editor state.
            enable_display: If True, enable limit line display.
            enable_test: If True, enable limit testing.
            check_errors: If True, check for instrument errors after writing limit data.

        Raises:
            ValueError: If segments are empty, invalid type values, non-finite numbers,
                       or if f_stop_hz <= f_start_hz.
            InstrumentError: If instrument reports an error after writing limit data
                            (only if check_errors=True).
        """
        # Validate + flatten (raises ValueError before anything is written)
        payload = format_limit_payload(table, include_count=True)

        if meas_name is not None:
            # Select the measurement/trace you want the limits applied to
            self.select_trace(meas_name, ch=ch)

        if check_errors:
            # Start from a clean status/error state so *ESR? below reflects
            # only this load, not stale errors from earlier commands.
            self.write("*CLS")

        if clear_first:
            self.write(f"CALC{ch}:LIM:DATA 0")

        # One-shot bulk write, matching the sample's "NumSeg then comma stream" pattern.
        self.write(f"CALC{ch}:LIM:DATA {payload}")

        # Wait for command completion and check for errors
        if check_errors:
            self.opc()  # wait for command completion
            self.check_esr("Limit table load failed")

        if enable_display:
            self.write(f"CALC{ch}:LIM:DISP ON")
        if enable_test:
            self.write(f"CALC{ch}:LIM ON")

    def query_limit_table(self, ch: int = 1, meas_name: Optional[str] = None) -> List[LimitSeg]:
        """
        Query and parse the current limit table from the instrument.

        Limit tables are per-trace: this reads the selected trace's table.

        Args:
            ch: Channel number (default: 1).
            meas_name: Optional trace to select first.

        Returns:
            List of limit segments as tuples (type, f_start_hz, f_stop_hz, y_start, y_stop).
            Empty list if no limits are defined.

        Raises:
            InstrumentError: If the instrument reports an error.
        """
        if meas_name is not None:
            self.select_trace(meas_name, ch=ch)
        self.write("FORM:DATA ASC")
        values = self.query_values(f"CALC{ch}:LIM:DATA?")

        if not values:
            return []

        # First value is the count
        count = int(values[0])
        if count == 0:
            return []

        # Remaining values are groups of 5: type, f_start, f_stop, y_start, y_stop
        segments = []
        for i in range(count):
            idx = 1 + i * 5
            if idx + 4 >= len(values):
                break
            typ = int(values[idx])
            f_start = float(values[idx + 1])
            f_stop = float(values[idx + 2])
            y_start = float(values[idx + 3])
            y_stop = float(values[idx + 4])
            segments.append((typ, f_start, f_stop, y_start, y_stop))

        return segments

    # sweep & data
    def single_sweep(self, ch: int = 1) -> None:
        """Run one sweep on a channel and block until it completes.

        Uses the E5071C bus-trigger pattern: trigger source BUS, continuous
        initiate ON (arms the channel), then :TRIG:SING which *OPC? waits on.

        Note: *TRG with INIT:CONT OFF (the previous implementation) fails on
        this instrument — *TRG errors unless the source is BUS, and a held
        channel never re-arms.
        """
        self.write("TRIG:SOUR BUS")
        self.write(f"INIT{ch}:CONT ON")
        self.write("TRIG:SING")
        self.opc()

    def fetch_fdata_pairs(self, ch: int = 1) -> List[Tuple[float, float]]:
        self.write("FORM:DATA ASC")
        raw = self.query_values(f"CALC{ch}:DATA:FDATA?")
        return list(zip(raw[0::2], raw[1::2]))

    def fetch_frequencies(self, ch: int = 1) -> List[float]:
        """Read the stimulus (frequency) axis for a channel, in Hz."""
        self.write("FORM:DATA ASC")
        return self.query_values(f"SENS{ch}:FREQ:DATA?")

    # markers (Phase-1)
    def marker_enable(self, idx: int, ch: int = 1, on: bool = True) -> None:
        """Enable or disable a marker.

        Args:
            idx: Marker number (1-9, or 10 for reference marker).
            ch: Channel number.
            on: True to enable, False to disable.
        """
        self.write(f"CALC{ch}:MARK{idx}:STAT {'ON' if on else 'OFF'}")

    def marker_set_function(self, idx: int, func: MarkerFunc, ch: int = 1) -> None:
        """Set the marker search function type and execute the search.

        Args:
            idx: Marker number (1-9, or 10 for reference marker).
            func: Search function type (MarkerFunc.MIN or MarkerFunc.MAX).
            ch: Channel number.
        """
        self.write(f"CALC{ch}:MARK{idx}:FUNC:TYPE {func.value}")
        self.write(f"CALC{ch}:MARK{idx}:FUNC:EXEC")

    def marker_set_range(self, f_start_hz: float, f_stop_hz: float, ch: int = 1) -> None:
        """Set the marker search domain range and enable it.

        The search domain range is per-channel on the E5071C, not per-marker.
        All markers on a channel share this range.  Calling this with a
        different band on a channel that already has one set will raise
        InstrumentError — call marker_clear_domain(ch) first to change bands.

        Args:
            f_start_hz: Start frequency in Hz.
            f_stop_hz: Stop frequency in Hz.
            ch: Channel number.

        Raises:
            ValueError: If f_stop_hz <= f_start_hz.
            InstrumentError: If a different band is already set on this channel.
        """
        if f_stop_hz <= f_start_hz:
            raise ValueError(
                f"f_stop_hz must be > f_start_hz (got {f_start_hz} .. {f_stop_hz})"
            )
        band = (f_start_hz, f_stop_hz)
        prev = self._marker_domain.get(ch)
        if prev is not None and prev != band:
            raise InstrumentError(
                f"Channel {ch} marker domain already set to "
                f"{prev[0]:.6g}–{prev[1]:.6g} Hz. "
                f"Call marker_clear_domain({ch}) before changing band."
            )
        self.write(f"CALC{ch}:MARK:FUNC:DOM:STAR {f_start_hz:.12g}")
        self.write(f"CALC{ch}:MARK:FUNC:DOM:STOP {f_stop_hz:.12g}")
        self.write(f"CALC{ch}:MARK:FUNC:DOM ON")
        self._marker_domain[ch] = band

    def marker_clear_domain(self, ch: int = 1) -> None:
        """Clear the marker search domain for a channel.

        Must be called before marker_set_range if changing the band
        on a channel that already has a domain set.

        Args:
            ch: Channel number.
        """
        self.write(f"CALC{ch}:MARK:FUNC:DOM OFF")
        self._marker_domain.pop(ch, None)

    def marker_configure(self, spec: MarkerSpec, ch: int = 1) -> None:
        """Configure a marker from a MarkerSpec.

        Orchestrates: enable -> set range (if enabled) -> set function + exec.
        For individual control use the primitive methods directly.

        Does NOT call opc().  Caller must sync after configuring all markers::

            vna.marker_configure(spec1)
            vna.marker_configure(spec2)
            vna.opc()          # single sync point

        Warning:
            Markers bind to the currently selected trace (CALC:PAR:SEL).
            Call select_trace() before marker_configure() to ensure
            correct binding.

        Args:
            spec: Marker specification.
            ch: Channel number.
        """
        self.marker_enable(spec.index, ch=ch, on=True)
        if spec.enable_domain:
            self.marker_set_range(spec.f_start_hz, spec.f_stop_hz, ch=ch)
        self.marker_set_function(spec.index, spec.func, ch=ch)

    def marker_read(self, idx: int, ch: int = 1) -> Tuple[float, float]:
        """Read the marker's X (stimulus) and Y (response) values.

        Args:
            idx: Marker number (1-9, or 10 for reference marker).
            ch: Channel number.

        Returns:
            (x_value, y_value) where x is the stimulus (freq in Hz)
            and y is the primary response value.

        Note:
            E5071C MARK:Y? returns two values (primary, secondary).
            Only the primary is returned here. Smith/Polar formats
            need a dedicated method in Phase-2.

        Warning:
            Returns values for the marker on the currently selected
            trace.  If the wrong trace is active, values will be valid
            but for the wrong measurement.
        """
        x = float(self.query(f"CALC{ch}:MARK{idx}:X?").strip())
        parts = self.query(f"CALC{ch}:MARK{idx}:Y?").strip().split(",")
        y = float(parts[0])
        return (x, y)
