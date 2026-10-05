# tests/test_drivers.py
"""Offline driver tests against a fake SCPI session (no pyvisa/hardware).

Run:  python tests/test_drivers.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vna_library import (  # noqa: E402
    E5071C, PNAFamily, VNA, driver_for_idn, VNAError, InstrumentError,
    SweepConfig, TraceSpec, EquationSpec, MarkerSpec, MarkerFunc,
    LimitTable, LimitSegment, LimitType,
)


class FakeSession:
    """Records writes; answers queries from a canned table."""

    def __init__(self, responses=None):
        self.writes: list[str] = []
        self.responses = {
            "*OPC?": "1",
            "*ESR?": "0",
            "SYST:ERR?": '+0,"No error"',
            "*IDN?": "Fake,Instrument,0,0",
            "CALC1:MARK1:X?": "1.23e9",
            "CALC1:MARK1:Y?": "-12.5,0.0",
            **(responses or {}),
        }

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        self.writes.append(cmd)
        return self.responses[cmd]

    def query_ascii_values(self, cmd):
        self.writes.append(cmd)
        return [float(v) for v in self.responses[cmd].split(",")]


TABLE = LimitTable.from_iter([
    LimitSegment(LimitType.MAX, 1.0e9, 2.0e9, -10.0, -10.0),
])


def test_interface_coverage():
    # Both drivers are concrete (no abstract methods left unimplemented).
    for cls in (E5071C, PNAFamily):
        vna = cls(FakeSession())
        assert isinstance(vna, VNA)


def test_e5071c_commands():
    s = FakeSession()
    vna = E5071C(s)
    vna.preset()
    vna.apply_sweep(SweepConfig(1.0e9, 2.0e9, points=201, avg_count=4))
    vna.define_trace(TraceSpec("Tr1", "S11"))
    vna.define_equation(EquationSpec("EQ1", "sqrt(abs(S11)^2)"))
    vna.apply_limit_table_bulk(TABLE, meas_name="Tr1")
    vna.single_sweep()
    vna.marker_configure(MarkerSpec(1, 1.1e9, 1.9e9, MarkerFunc.MAX))
    x, y = vna.marker_read(1)
    assert (x, y) == (1.23e9, -12.5)

    w = s.writes
    assert "SYST:PRES" in w
    assert "SENS1:AVER:COUN 4" in w
    assert 'CALC1:PAR:DEF "Tr1","S11"' in w
    assert 'CALC1:EQU:DEF "sqrt(abs(S11)^2)"' in w
    assert "CALC1:LIM:DEL:ALL" in w
    assert "CALC1:LIM:DATA 1,1,1000000000,2000000000,-10,-10" in w
    assert "CALC1:LIM:DISP ON" in w and "CALC1:LIM:STAT ON" in w
    assert "*TRG" in w
    assert "CALC1:MARK:FUNC:DOM:STAR 1100000000" in w
    assert "CALC1:MARK1:FUNC:TYPE MAX" in w and "CALC1:MARK1:FUNC:EXEC" in w

    # Per-channel domain: a different band on the same channel must raise.
    try:
        vna.marker_set_range(1.2e9, 1.8e9)
    except InstrumentError:
        pass
    else:
        raise AssertionError("expected InstrumentError on domain conflict")
    vna.marker_clear_domain()
    vna.marker_set_range(1.2e9, 1.8e9)  # fine after clearing


def test_pna_family_commands():
    s = FakeSession()
    vna = PNAFamily(s)
    vna.preset()
    vna.apply_sweep(SweepConfig(1.0e9, 2.0e9))
    vna.define_trace(TraceSpec("Tr1", "S11"))
    vna.define_equation(EquationSpec("EQ1", "sqrt(abs(S11)^2)"))
    vna.apply_limit_table_bulk(TABLE, meas_name="Tr1")
    vna.single_sweep()
    # Per-marker user ranges: two markers may hold different bands at once.
    vna.marker_configure(MarkerSpec(1, 1.1e9, 1.3e9, MarkerFunc.MAX))
    vna.marker_configure(MarkerSpec(2, 1.5e9, 1.9e9, MarkerFunc.MIN))
    x, y = vna.marker_read(1)
    assert (x, y) == (1.23e9, -12.5)

    w = s.writes
    assert 'CALC1:PAR:DEF:EXT "Tr1","S11"' in w
    assert 'CALC1:EQU:TEXT "sqrt(abs(S11)^2)"' in w
    assert "CALC1:LIM:SEGM1:TYPE MAX" in w
    assert "CALC1:LIM:SEGM1:STIM:STAR 1000000000" in w
    assert "CALC1:LIM:SEGM1:AMPL:STOP -10" in w
    assert "SENS1:SWE:MODE SING" in w
    assert "CALC1:MARK1:FUNC:DOM:USER 1" in w
    assert "CALC1:MARK2:FUNC:DOM:USER:STAR 1500000000" in w
    assert "CALC1:MARK1:FUNC:EXEC MAX" in w
    assert "CALC1:MARK2:FUNC:EXEC MIN" in w

    # limits_clear switches off exactly the segments this driver wrote.
    vna.limits_clear()
    assert "CALC1:LIM:SEGM1:TYPE OFF" in w

    # Range without a marker number is a usage error on this family.
    try:
        vna.marker_set_range(1.0e9, 2.0e9)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError without marker=")


def test_error_check_raises():
    s = FakeSession({"*ESR?": "32", "SYST:ERR?": '-113,"Undefined header"'})
    # get_errors would loop on a non-zero response table; feed one error then ok
    errs = iter(['-113,"Undefined header"', '+0,"No error"'])
    s.responses = dict(s.responses)
    s.query = lambda cmd, _e=errs, _s=s: (
        _s.writes.append(cmd) or (next(_e) if cmd == "SYST:ERR?" else
                                  {"*OPC?": "1", "*ESR?": "32"}[cmd]))
    vna = PNAFamily(s)
    try:
        vna.apply_limit_table_bulk(TABLE)
    except InstrumentError as e:
        assert "Undefined header" in str(e)
    else:
        raise AssertionError("expected InstrumentError")


def test_factory_dispatch():
    cases = {
        "Agilent Technologies,E5071C,MY46107071,A.11.30": E5071C,
        "Keysight Technologies,E5080B,MY12345678,B.15.20": PNAFamily,
        "Keysight Technologies,E5081A,...": PNAFamily,
        "Keysight Technologies,N5222B,...": PNAFamily,
        "Keysight Technologies,P5004A,...": PNAFamily,
        "Keysight Technologies,M9804A,...": PNAFamily,
        "Keysight Technologies,E5061B,...": E5071C,
    }
    for idn, cls in cases.items():
        assert driver_for_idn(idn) is cls, idn
    try:
        driver_for_idn("Rohde&Schwarz,ZNB8,...")
    except VNAError:
        pass
    else:
        raise AssertionError("expected VNAError for unknown model")


def test_sweep_values_roundtrip():
    s = FakeSession({"CALC1:DATA? FDATA": "-1.0,-2.0,-3.0",
                     "CALC1:X?": "1e9,1.5e9,2e9"})
    vna = PNAFamily(s)
    assert vna.fetch_fdata() == [-1.0, -2.0, -3.0]
    assert vna.fetch_stimulus() == [1e9, 1.5e9, 2e9]

    s2 = FakeSession({"CALC1:DATA:FDATA?": "-1.0,0.0,-2.0,0.0",
                      "SENS1:FREQ:DATA?": "1e9,2e9"})
    vna2 = E5071C(s2)
    assert vna2.fetch_fdata_pairs() == [(-1.0, 0.0), (-2.0, 0.0)]
    assert vna2.fetch_fdata() == [-1.0, -2.0]
    assert vna2.fetch_stimulus() == [1e9, 2e9]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
