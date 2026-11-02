"""Stateful instrument simulators for behavior-level testing.

These model enough instrument state to catch *semantic* driver bugs that
string-assertion tests cannot: which trace a limit table lands on, shared
marker search ranges, trigger-source requirements, and error-queue /
status-register behavior.  They are deliberately strict: an unknown command
pushes -113 "Undefined header" into the error queue and sets the ESR command
-error bit, exactly the failure mode that bit real hardware.

They implement the VisaSession protocol, so drivers run against them
unchanged.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple


class _SimulatorBase:
    """Common IEEE-488.2 plumbing: error queue, ESR, triggering, data."""

    IDN = "Keysight Technologies,SIMULATED,MY00000000,A.00.00"

    def __init__(self, points: int = 11, f_start: float = 1.0e9, f_stop: float = 2.0e9):
        self.writes: List[str] = []
        self.timeout = 30000
        self.closed = False
        self.points = points
        self.f_start = f_start
        self.f_stop = f_stop
        self.error_queue: List[str] = []
        self.esr = 0
        self.trig_source = "INT"
        self.sweeps_run = 0

    # --- VisaSession protocol ---
    def close(self) -> None:
        self.closed = True

    def write(self, cmd: str) -> None:
        self.writes.append(cmd)
        result = self._execute(cmd.strip())
        if result is not None:
            # A query sent via write(): harmless for the simulator.
            pass

    def query(self, cmd: str) -> str:
        self.writes.append(cmd)
        result = self._execute(cmd.strip())
        if result is None:
            self._error(-110, "Command header error (write used as query)")
            return ""
        return result

    def query_ascii_values(self, cmd: str) -> List[float]:
        raw = self.query(cmd).strip().strip('"')
        if not raw:
            return []
        return [float(x) for x in raw.split(",")]

    # --- shared behavior ---
    def _error(self, code: int, msg: str) -> None:
        self.error_queue.append(f'{code},"{msg}"')
        if code == -113:
            self.esr |= 0b00100000  # command error
        else:
            self.esr |= 0b00010000  # execution error

    def _frequencies(self) -> List[float]:
        n = self.points
        if n == 1:
            return [self.f_start]
        step = (self.f_stop - self.f_start) / (n - 1)
        return [self.f_start + i * step for i in range(n)]

    def _execute(self, cmd: str) -> Optional[str]:
        # IEEE-488.2 common commands
        if cmd == "*IDN?":
            return self.IDN
        if cmd == "*OPC?":
            return "1"
        if cmd == "*ESR?":
            val, self.esr = self.esr, 0  # ESR clears on read
            return str(val)
        if cmd == "*CLS":
            self.error_queue.clear()
            self.esr = 0
            return None
        if cmd == "*RST":
            self._reset_state()
            return None
        if cmd == "SYST:PRES":
            self._reset_state()
            return None
        if cmd == "SYST:ERR?":
            if self.error_queue:
                return self.error_queue.pop(0)
            return '+0,"No error"'
        if cmd.startswith("FORM:DATA"):
            return None
        m = re.fullmatch(r"TRIG:SOUR (\w+)", cmd)
        if m:
            self.trig_source = m.group(1).upper()[:3]
            return None
        # Sweep setup: accept and record frequency plan
        m = re.fullmatch(r"SENS(\d+):FREQ:STAR ([\d.eE+-]+)", cmd)
        if m:
            self.f_start = float(m.group(2))
            return None
        m = re.fullmatch(r"SENS(\d+):FREQ:STOP ([\d.eE+-]+)", cmd)
        if m:
            self.f_stop = float(m.group(2))
            return None
        m = re.fullmatch(r"SENS(\d+):SWE:POIN (\d+)", cmd)
        if m:
            self.points = int(m.group(2))
            return None
        if re.fullmatch(r"SENS\d+:(BWID|AVER:STAT|AVER:COUN) .+", cmd) or re.fullmatch(
            r"SOUR\d+:POW .+", cmd
        ):
            return None
        return self._execute_dialect(cmd)

    def _reset_state(self) -> None:
        self.trig_source = "INT"

    def _execute_dialect(self, cmd: str) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError


class SimulatedE5071C(_SimulatorBase):
    """Behavioral model of the E5071C ENA dialect.

    State modeled: named traces per channel, the SELECTED trace (which is
    what every CALC:LIM / CALC:EQU / marker command actually acts on),
    per-trace limit tables with the count-prefixed wire format, the single
    per-channel marker search domain, equations, and BUS-trigger-only
    single sweeps.  There is no CALC:LIM:DEL:ALL and no CALC:PAR:DEL:ALL —
    both produce -113 like the real instrument.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self._reset_state()

    def _reset_state(self) -> None:
        super()._reset_state()
        self.traces: Dict[int, Dict[str, str]] = {}      # ch -> {name: sparam}
        self.selected: Dict[int, str] = {}               # ch -> trace name
        self.limits: Dict[Tuple[int, str], List[float]] = {}   # (ch, trace) -> flat segs
        self.limit_disp: Dict[Tuple[int, str], bool] = {}
        self.limit_test: Dict[Tuple[int, str], bool] = {}
        self.equations: Dict[Tuple[int, str], str] = {}
        self.eq_state: Dict[Tuple[int, str], bool] = {}
        self.marker_domain: Dict[int, Tuple[float, float]] = {}  # ch -> (star, stop)
        self.domain_on: Dict[int, bool] = {}
        self.markers: Dict[Tuple[int, int], float] = {}  # (ch, idx) -> x position
        self.init_cont: Dict[int, bool] = {}

    def _sel(self, ch: int) -> Optional[str]:
        name = self.selected.get(ch)
        if name is None:
            self._error(-221, "Settings conflict (no trace selected)")
        return name

    def _execute_dialect(self, cmd: str) -> Optional[str]:
        m = re.fullmatch(r'CALC(\d+):PAR:DEF "([^"]+)","([^"]+)"', cmd)
        if m:
            ch, name, par = int(m.group(1)), m.group(2), m.group(3)
            self.traces.setdefault(ch, {})[name] = par
            return None
        m = re.fullmatch(r"CALC(\d+):PAR:COUN (\d+)", cmd)
        if m:
            ch, n = int(m.group(1)), int(m.group(2))
            names = list(self.traces.get(ch, {}))
            for name in names[n:]:
                del self.traces[ch][name]
                self.limits.pop((ch, name), None)
            return None
        m = re.fullmatch(r'CALC(\d+):PAR:SEL "([^"]+)"', cmd)
        if m:
            ch, name = int(m.group(1)), m.group(2)
            if name not in self.traces.get(ch, {}):
                self._error(-224, "Illegal parameter value (trace not found)")
            else:
                self.selected[ch] = name
            return None
        m = re.fullmatch(r'DISP:WIND(\d+):TRAC(\d+):FEED "([^"]+)"', cmd)
        if m:
            return None
        if re.fullmatch(r"CALC(\d+):FORM \w+", cmd):
            return None
        # equations (selected trace)
        m = re.fullmatch(r'CALC(\d+):EQU:TEXT "([^"]*)"', cmd)
        if m:
            ch = int(m.group(1))
            name = self._sel(ch)
            if name:
                self.equations[(ch, name)] = m.group(2)
            return None
        m = re.fullmatch(r"CALC(\d+):EQU:VAL\?", cmd)
        if m:
            ch = int(m.group(1))
            name = self.selected.get(ch)
            expr = self.equations.get((ch, name), "")
            return "1" if expr and expr.count("(") == expr.count(")") else "0"
        m = re.fullmatch(r"CALC(\d+):EQU:STAT (ON|OFF|1|0)", cmd)
        if m:
            ch = int(m.group(1))
            name = self._sel(ch)
            if name:
                self.eq_state[(ch, name)] = m.group(2) in ("ON", "1")
            return None
        # limits (selected trace, count-prefixed wire format)
        m = re.fullmatch(r"CALC(\d+):LIM:DATA\?", cmd)
        if m:
            ch = int(m.group(1))
            name = self.selected.get(ch)
            table = self.limits.get((ch, name), [])
            count = len(table) // 5
            return ",".join([str(count)] + [f"{v:.12g}" for v in table])
        m = re.fullmatch(r"CALC(\d+):LIM:DATA (.+)", cmd)
        if m:
            ch = int(m.group(1))
            values = [float(x) for x in m.group(2).split(",")]
            count = int(values[0])
            if len(values) - 1 != count * 5:
                self._error(-109, "Missing parameter (limit count mismatch)")
                return None
            name = self._sel(ch)
            if name:
                self.limits[(ch, name)] = values[1:]
            return None
        m = re.fullmatch(r"CALC(\d+):LIM:DISP (ON|OFF)", cmd)
        if m:
            ch = int(m.group(1))
            name = self._sel(ch)
            if name:
                self.limit_disp[(ch, name)] = m.group(2) == "ON"
            return None
        m = re.fullmatch(r"CALC(\d+):LIM(:STAT)? (ON|OFF)", cmd)
        if m:
            ch = int(m.group(1))
            name = self._sel(ch)
            if name:
                self.limit_test[(ch, name)] = m.group(3) == "ON"
            return None
        # markers
        m = re.fullmatch(r"CALC(\d+):MARK(\d+):STAT (ON|OFF)", cmd)
        if m:
            return None
        m = re.fullmatch(r"CALC(\d+):MARK:FUNC:DOM:STAR ([\d.eE+-]+)", cmd)
        if m:
            ch = int(m.group(1))
            star, stop = self.marker_domain.get(ch, (self.f_start, self.f_stop))
            self.marker_domain[ch] = (float(m.group(2)), stop)
            return None
        m = re.fullmatch(r"CALC(\d+):MARK:FUNC:DOM:STOP ([\d.eE+-]+)", cmd)
        if m:
            ch = int(m.group(1))
            star, stop = self.marker_domain.get(ch, (self.f_start, self.f_stop))
            self.marker_domain[ch] = (star, float(m.group(2)))
            return None
        m = re.fullmatch(r"CALC(\d+):MARK:FUNC:DOM (ON|OFF)", cmd)
        if m:
            self.domain_on[int(m.group(1))] = m.group(2) == "ON"
            return None
        m = re.fullmatch(r"CALC(\d+):MARK(\d+):FUNC:TYPE (\w+)", cmd)
        if m:
            ch, idx = int(m.group(1)), int(m.group(2))
            self.markers[(ch, idx, "func")] = m.group(3)  # type: ignore[index]
            return None
        m = re.fullmatch(r"CALC(\d+):MARK(\d+):FUNC:EXEC", cmd)
        if m:
            ch, idx = int(m.group(1)), int(m.group(2))
            if self.domain_on.get(ch):
                star, stop = self.marker_domain[ch]
            else:
                star, stop = self.f_start, self.f_stop
            func = self.markers.get((ch, idx, "func"), "MIN")  # type: ignore[index]
            self.markers[(ch, idx)] = star if func == "MIN" else stop
            return None
        m = re.fullmatch(r"CALC(\d+):MARK(\d+):X\?", cmd)
        if m:
            ch, idx = int(m.group(1)), int(m.group(2))
            return f"{self.markers.get((ch, idx), self.f_start):.12g}"
        m = re.fullmatch(r"CALC(\d+):MARK(\d+):Y\?", cmd)
        if m:
            return "-10.0,0.0"
        # sweep & data
        m = re.fullmatch(r"INIT(\d+):CONT (ON|OFF)", cmd)
        if m:
            self.init_cont[int(m.group(1))] = m.group(2) == "ON"
            return None
        if cmd == "*TRG" or cmd == "TRIG:SING":
            if self.trig_source != "BUS":
                self._error(-211, "Trigger ignored (source not BUS)")
            else:
                self.sweeps_run += 1
            return None
        m = re.fullmatch(r"SENS(\d+):FREQ:DATA\?", cmd)
        if m:
            return ",".join(f"{f:.12g}" for f in self._frequencies())
        m = re.fullmatch(r"CALC(\d+):DATA:FDATA\?", cmd)
        if m:
            return ",".join("-3.0,0.0" for _ in range(self.points))
        self._error(-113, "Undefined header")
        return "" if cmd.endswith("?") else None


class SimulatedPNA(_SimulatorBase):
    """Behavioral model of the PNA / new-platform dialect.

    State modeled: measurements by number (global numbers, each owned by a
    channel, like the real platform), windows that must exist before FEED,
    per-measurement limit tables with the count-less wire format and
    LIM:DATA:DEL, CHANNEL-GLOBAL marker user ranges (the collision hazard),
    equations with validity, internal-trigger-only single sweeps, and the
    instrument-wide CALC:PAR:DEL:ALL.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self._reset_state()

    def _reset_state(self) -> None:
        super()._reset_state()
        self.meas: Dict[int, Tuple[int, str]] = {}       # mnum -> (ch, param)
        self.windows: set[int] = set()
        self.limits: Dict[int, List[float]] = {}         # mnum -> flat segs
        self.limit_disp: Dict[int, bool] = {}
        self.limit_test: Dict[int, bool] = {}
        self.equations: Dict[int, str] = {}
        self.eq_state: Dict[int, bool] = {}
        self.user_ranges: Dict[Tuple[int, int], Tuple[float, float]] = {}  # (ch, rng) -> span
        self.marker_range: Dict[Tuple[int, int], int] = {}  # (mnum, mkr) -> rng
        self.markers: Dict[Tuple[int, int], float] = {}     # (mnum, mkr) -> x

    def _meas(self, mnum: int) -> Optional[Tuple[int, str]]:
        entry = self.meas.get(mnum)
        if entry is None:
            self._error(-224, "Illegal parameter value (measurement not found)")
        return entry

    def _execute_dialect(self, cmd: str) -> Optional[str]:
        m = re.fullmatch(r'CALC(\d+):MEAS(\d+):DEF "([^"]+)"', cmd)
        if m:
            ch, mnum = int(m.group(1)), int(m.group(2))
            self.meas[mnum] = (ch, m.group(3))
            return None
        m = re.fullmatch(r"CALC(\d*):?PAR:DEL:ALL", cmd)
        if m:
            # Instrument-wide, regardless of any channel prefix.
            self.meas.clear()
            self.limits.clear()
            self.marker_range.clear()
            return None
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):DEL", cmd)
        if m:
            mnum = int(m.group(2))
            if self._meas(mnum):
                del self.meas[mnum]
                self.limits.pop(mnum, None)
            return None
        m = re.fullmatch(r"SYST:MEAS:CAT\?(?: (\d+))?", cmd)
        if m:
            ch = int(m.group(1)) if m.group(1) else None
            nums = sorted(n for n, (c, _) in self.meas.items() if ch is None or c == ch)
            return '"' + ",".join(str(n) for n in nums) + '"'
        m = re.fullmatch(r"DISP:WIND(\d+) ON", cmd)
        if m:
            self.windows.add(int(m.group(1)))
            return None
        m = re.fullmatch(r"DISP:MEAS(\d+):FEED (\d+)", cmd)
        if m:
            mnum, wnum = int(m.group(1)), int(m.group(2))
            if wnum not in self.windows:
                self._error(-224, "Illegal parameter value (window does not exist)")
            else:
                self._meas(mnum)
            return None
        if re.fullmatch(r"CALC\d+:MEAS\d+:FORM \w+", cmd):
            return None
        # equations
        m = re.fullmatch(r'CALC(\d+):MEAS(\d+):EQU:TEXT "([^"]*)"', cmd)
        if m:
            mnum = int(m.group(2))
            if self._meas(mnum):
                self.equations[mnum] = m.group(3)
            return None
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):EQU:VAL\?", cmd)
        if m:
            expr = self.equations.get(int(m.group(2)), "")
            return "1" if expr and expr.count("(") == expr.count(")") else "0"
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):EQU:STAT (ON|OFF|1|0)", cmd)
        if m:
            self.eq_state[int(m.group(2))] = m.group(3) in ("ON", "1")
            return None
        # limits (count-less wire format, per measurement)
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):LIM:DATA\?", cmd)
        if m:
            table = self.limits.get(int(m.group(2)), [])
            return ",".join(f"{v:.12g}" for v in table) if table else "0"
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):LIM:DATA:DEL", cmd)
        if m:
            mnum = int(m.group(2))
            if self._meas(mnum):
                self.limits.pop(mnum, None)
            return None
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):LIM:DATA (.+)", cmd)
        if m:
            mnum = int(m.group(2))
            values = [float(x) for x in m.group(3).split(",")]
            if len(values) % 5 != 0:
                self._error(-109, "Missing parameter (limit data not groups of 5)")
                return None
            if self._meas(mnum):
                self.limits[mnum] = values
            return None
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):LIM:DISP (ON|OFF)", cmd)
        if m:
            self.limit_disp[int(m.group(2))] = m.group(3) == "ON"
            return None
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):LIM:STAT (ON|OFF)", cmd)
        if m:
            self.limit_test[int(m.group(2))] = m.group(3) == "ON"
            return None
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):LIM:FAIL\?", cmd)
        if m:
            return "0"
        # markers: user ranges are per CHANNEL
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):MARK(\d+):STAT (ON|OFF)", cmd)
        if m:
            return None
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):MARK(\d+):FUNC:DOM:USER (\d+)", cmd)
        if m:
            self.marker_range[(int(m.group(2)), int(m.group(3)))] = int(m.group(4))
            return None
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):MARK(\d+):FUNC:DOM:USER:STAR ([\d.eE+-]+)", cmd)
        if m:
            ch, mnum, mkr = int(m.group(1)), int(m.group(2)), int(m.group(3))
            rng = self.marker_range.get((mnum, mkr), 0)
            if rng == 0:
                self._error(-221, "Settings conflict (cannot set span of full-span range 0)")
                return None
            star, stop = self.user_ranges.get((ch, rng), (self.f_start, self.f_stop))
            self.user_ranges[(ch, rng)] = (float(m.group(4)), stop)
            return None
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):MARK(\d+):FUNC:DOM:USER:STOP ([\d.eE+-]+)", cmd)
        if m:
            ch, mnum, mkr = int(m.group(1)), int(m.group(2)), int(m.group(3))
            rng = self.marker_range.get((mnum, mkr), 0)
            if rng == 0:
                self._error(-221, "Settings conflict (cannot set span of full-span range 0)")
                return None
            star, stop = self.user_ranges.get((ch, rng), (self.f_start, self.f_stop))
            self.user_ranges[(ch, rng)] = (star, float(m.group(4)))
            return None
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):MARK(\d+):FUNC:EXEC (\w+)", cmd)
        if m:
            ch, mnum, mkr = int(m.group(1)), int(m.group(2)), int(m.group(3))
            rng = self.marker_range.get((mnum, mkr), 0)
            star, stop = (
                self.user_ranges.get((ch, rng), (self.f_start, self.f_stop))
                if rng
                else (self.f_start, self.f_stop)
            )
            self.markers[(mnum, mkr)] = star if m.group(4).upper().startswith("MIN") else stop
            return None
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):MARK(\d+):X\?", cmd)
        if m:
            return f"{self.markers.get((int(m.group(2)), int(m.group(3))), self.f_start):.12g}"
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):MARK(\d+):Y\?", cmd)
        if m:
            return "-10.0,0.0"
        # sweep & data
        m = re.fullmatch(r"SENS(\d+):SWE:MODE SING\w*", cmd)
        if m:
            if self.trig_source not in ("INT", "IMM"):
                self._error(-221, "Settings conflict (SWE:MODE SINGle needs internal trigger)")
            else:
                self.sweeps_run += 1
            return None
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):X:VAL\?", cmd)
        if m:
            if self._meas(int(m.group(2))):
                return ",".join(f"{f:.12g}" for f in self._frequencies())
            return ""
        m = re.fullmatch(r"CALC(\d+):MEAS(\d+):DATA:FDATA\?", cmd)
        if m:
            if self._meas(int(m.group(2))):
                return ",".join("-3.0" for _ in range(self.points))
            return ""
        self._error(-113, "Undefined header")
        return "" if cmd.endswith("?") else None
