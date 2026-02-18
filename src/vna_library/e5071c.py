# src/vna_library/e5071c.py
from __future__ import annotations
from dataclasses import dataclass
import math
from typing import Iterable, List, Sequence, Tuple, Optional

import pyvisa

from .models import SweepConfig, TraceSpec, EquationSpec, LimitTable, MarkerSpec, MarkerFunc, LimitType

# (type, f_start_hz, f_stop_hz, y_start, y_stop)
LimitSeg = Tuple[int, float, float, float, float]

class VNAError(Exception): ...
class InstrumentError(VNAError): ...
class VNATimeout(VNAError): ...


class E5071C:
    def __init__(self, resource: str, timeout_ms: int = 30000):
        rm = pyvisa.ResourceManager()
        self.inst = rm.open_resource(resource)
        self.inst.timeout = timeout_ms
        self.inst.write_termination = "\n"
        self.inst.read_termination = "\n"
        self._marker_domain: dict[int, tuple[float, float]] = {}  # ch → (start, stop)

    # low-level
    def write(self, cmd: str) -> None:
        self.inst.write(cmd)

    def query(self, cmd: str) -> str:
        return self.inst.query(cmd)

    def query_values(self, cmd: str) -> List[float]:
        return self.inst.query_ascii_values(cmd)

    def opc(self) -> None:
        self.query("*OPC?")

    def get_errors(self) -> List[str]:
        """
        Query and return all errors from the instrument error queue.
        
        Returns:
            List of error strings. Empty list if no errors.
        """
        errors = []
        while True:
            err = self.query("SYST:ERR?").strip()
            if err.startswith("+0"):
                break
            errors.append(err)
        return errors

    # basics
    def idn(self) -> str:
        return self.query("*IDN?").strip()

    def preset(self) -> None:
        self.write("SYST:PRES")
        self._marker_domain.clear()

    def reset(self) -> None:
        self.write("*RST")
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
    def limits_on(self, ch: int = 1) -> None:
        self.write(f"CALC{ch}:LIM:STAT ON")

    def limits_clear(self, ch: int = 1) -> None:
        self.write(f"CALC{ch}:LIM:DEL:ALL")

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

        Args:
            table: LimitTable containing segments to apply.
            ch: Channel number (default: 1).
            meas_name: Optional measurement/trace name (e.g., "TR2", "EQ1"). 
                      If None, uses currently selected trace (caller must ensure correct selection).
            clear_first: If True, clear existing limits before applying new ones.
            enable_display: If True, enable limit line display.
            enable_test: If True, enable limit testing.
            check_errors: If True, check for instrument errors after writing limit data.

        Raises:
            ValueError: If segments are empty, invalid type values, non-finite numbers,
                       or if f_stop_hz <= f_start_hz.
            InstrumentError: If instrument reports an error after writing limit data
                            (only if check_errors=True).

        Note:
            This method requires :CALC{ch}:LIM:STAT ON to actually activate limit testing.
            The method enables this automatically when enable_test=True.
        """
        # Convert LimitTable to tuple format
        segments: Sequence[LimitSeg] = [
            (int(seg.kind), seg.f_start_hz, seg.f_stop_hz, seg.y_start_db, seg.y_stop_db)
            for seg in table.segments
        ]
        if not segments:
            raise ValueError("segments is empty")

        if meas_name is not None:
            # Select the measurement/trace you want the limits applied to

            self.write(f'CALC{ch}:PAR:SEL "{meas_name}"')

        # Validate + flatten
        flat: list[str] = [str(len(segments))]

        for i, (typ, fs, fe, ys, ye) in enumerate(segments, start=1):
            if typ not in (1, 2):
                raise ValueError(f"segment {i}: typ must be 1 (MAX) or 2 (MIN), got {typ}")
            for name, v in (("fs", fs), ("fe", fe), ("ys", ys), ("ye", ye)):
                if not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                    raise ValueError(f"segment {i}: {name} must be a finite number, got {v!r}")
            if fe <= fs:
                raise ValueError(f"segment {i}: f_stop_hz must be > f_start_hz (got {fs} .. {fe})")

            # Important: keep formatting “boring” (no thousands separators, no units).
            # .12g is usually safe for SCPI numeric parsing.
            flat.append(str(int(typ)))
            flat.append(f"{float(fs):.12g}")
            flat.append(f"{float(fe):.12g}")
            flat.append(f"{float(ys):.12g}")
            flat.append(f"{float(ye):.12g}")

        if clear_first:
            self.write(f"CALC{ch}:LIM:DEL:ALL")

        # One-shot bulk write, matching the sample's "NumSeg then comma stream" pattern.
        payload = ",".join(flat)
        self.write(f"CALC{ch}:LIM:DATA {payload}")

        # Wait for command completion and check for errors
        if check_errors:
            self.opc()  # wait for command completion
            esr = int(self.query("*ESR?"))
            if esr & 0b00000100:  # Command error bit (bit 2)
                errors = self.get_errors()
                raise InstrumentError(
                    f"Limit table load failed – instrument error: {errors[0] if errors else 'Unknown error'}"
                )

        if enable_display:
            self.write(f"CALC{ch}:LIM:DISP ON")
        if enable_test:
            self.write(f"CALC{ch}:LIM ON")
            # Critical: Enable limit state to activate testing/judgment
            self.write(f"CALC{ch}:LIM:STAT ON")

    def query_limit_table(self, ch: int = 1) -> List[LimitSeg]:
        """
        Query and parse the current limit table from the instrument.

        Args:
            ch: Channel number (default: 1).

        Returns:
            List of limit segments as tuples (type, f_start_hz, f_stop_hz, y_start, y_stop).
            Empty list if no limits are defined.

        Raises:
            InstrumentError: If the instrument reports an error.
        """
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
        self.write("INIT:CONT OFF")
        self.write("*TRG")
        self.opc()

    def fetch_fdata_pairs(self, ch: int = 1) -> List[Tuple[float, float]]:
        self.write("FORM:DATA ASC")
        raw = self.query_values(f"CALC{ch}:DATA:FDATA?")
        return list(zip(raw[0::2], raw[1::2]))

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
                f"{prev[0]:.6g}\u2013{prev[1]:.6g} Hz. "
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
