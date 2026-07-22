import pytest

from vna_library import (
    E5071C,
    InstrumentError,
    LimitSegment,
    LimitTable,
    LimitType,
    MarkerFunc,
    MarkerSpec,
    SweepConfig,
    TraceSpec,
)


def make_table():
    return LimitTable.from_iter(
        [
            LimitSegment(LimitType.MAX, 1.0e9, 2.0e9, -10.0, -10.0),
            LimitSegment(LimitType.MIN, 1.0e9, 2.0e9, -60.0, -60.0),
        ]
    )


def test_apply_sweep_emits_expected_scpi(vna, session):
    vna.apply_sweep(SweepConfig(start_hz=1e9, stop_hz=2e9, points=201, if_bw_hz=100.0, power_dbm=-5.0))
    assert session.writes == [
        "SENS1:FREQ:STAR 1000000000.0",
        "SENS1:FREQ:STOP 2000000000.0",
        "SENS1:SWE:POIN 201",
        "SENS1:BWID 100.0",
        "SENS1:AVER:STAT OFF",
        "SOUR1:POW -5.0",
    ]


def test_apply_sweep_enables_averaging(vna, session):
    vna.apply_sweep(SweepConfig(start_hz=1e9, stop_hz=2e9, avg_count=8))
    assert "SENS1:AVER:STAT ON" in session.writes
    assert "SENS1:AVER:COUN 8" in session.writes


def test_define_trace(vna, session):
    vna.define_trace(TraceSpec(name="Tr1", sparam="S11"))
    assert session.writes == [
        'CALC1:PAR:DEF "Tr1","S11"',
        'DISP:WIND1:TRAC1:FEED "Tr1"',
        "CALC1:FORM MLOG",
    ]


# --- limits -----------------------------------------------------------------

def test_limits_clear_uses_zero_segment_write_not_del_all(vna, session):
    vna.limits_clear(meas_name="Tr1")
    assert session.writes == ['CALC1:PAR:SEL "Tr1"', "CALC1:LIM:DATA 0"]
    assert not any("DEL:ALL" in w for w in session.writes)


def test_apply_limit_table_bulk_sequence(vna, session):
    vna.apply_limit_table_bulk(make_table(), meas_name="Tr1")
    # E5071C rejects CALC:LIM:DEL:ALL (PNA syntax) — it must never be sent.
    assert not any("LIM:DEL" in w for w in session.writes)
    assert session.writes == [
        'CALC1:PAR:SEL "Tr1"',
        "*CLS",
        "CALC1:LIM:DATA 0",
        "CALC1:LIM:DATA 2,1,1000000000,2000000000,-10,-10,2,1000000000,2000000000,-60,-60",
        "*OPC?",
        "*ESR?",
        "CALC1:LIM:DISP ON",
        "CALC1:LIM ON",
    ]


def test_apply_limit_table_bulk_enable_commands_follow_error_check(vna, session):
    """Regression: a queued instrument error must not leave limits loaded but
    display/test never enabled (the on-hardware 'limits not set on test' bug)."""
    session.responses["*ESR?"] = "32"  # execution error bit
    session.responses["SYST:ERR?"] = ['-113,"Undefined header"', '+0,"No error"']
    with pytest.raises(InstrumentError, match="Undefined header"):
        vna.apply_limit_table_bulk(make_table(), meas_name="Tr1")
    assert "CALC1:LIM:DISP ON" not in session.writes
    assert "CALC1:LIM ON" not in session.writes


def test_apply_limit_table_bulk_without_meas_name_uses_selection(vna, session):
    vna.apply_limit_table_bulk(make_table(), check_errors=False)
    assert not any("PAR:SEL" in w for w in session.writes)


def test_apply_limit_table_bulk_two_traces_reselects(vna, session):
    vna.apply_limit_table_bulk(make_table(), meas_name="Tr1", check_errors=False)
    vna.apply_limit_table_bulk(make_table(), meas_name="Tr2", check_errors=False)
    sel = [w for w in session.writes if "PAR:SEL" in w]
    assert sel == ['CALC1:PAR:SEL "Tr1"', 'CALC1:PAR:SEL "Tr2"']


def test_apply_limit_table_bulk_rejects_empty_table(vna):
    with pytest.raises(ValueError, match="empty"):
        vna.apply_limit_table_bulk(LimitTable(segments=()))


def test_query_limit_table_roundtrip(vna, session):
    session.responses["CALC1:LIM:DATA?"] = [
        2.0,
        1.0, 1.0e9, 2.0e9, -10.0, -10.0,
        2.0, 1.0e9, 2.0e9, -60.0, -60.0,
    ]
    segs = vna.query_limit_table()
    assert segs == [
        (1, 1.0e9, 2.0e9, -10.0, -10.0),
        (2, 1.0e9, 2.0e9, -60.0, -60.0),
    ]


def test_query_limit_table_empty(vna, session):
    session.responses["CALC1:LIM:DATA?"] = [0.0]
    assert vna.query_limit_table() == []


def test_query_limit_table_truncated_response(vna, session):
    # Count claims 2 segments but only one is present — parse what exists.
    session.responses["CALC1:LIM:DATA?"] = [2.0, 1.0, 1.0e9, 2.0e9, -10.0, -10.0]
    assert vna.query_limit_table() == [(1, 1.0e9, 2.0e9, -10.0, -10.0)]


# --- sweep & data -----------------------------------------------------------

def test_single_sweep_uses_bus_trigger(vna, session):
    vna.single_sweep()
    assert session.writes == ["TRIG:SOUR BUS", "INIT1:CONT ON", "TRIG:SING", "*OPC?"]


def test_fetch_fdata_pairs(vna, session):
    session.responses["CALC1:DATA:FDATA?"] = [-3.0, 0.0, -6.0, 0.0]
    assert vna.fetch_fdata_pairs() == [(-3.0, 0.0), (-6.0, 0.0)]


def test_fetch_frequencies(vna, session):
    session.responses["SENS1:FREQ:DATA?"] = [1.0e9, 1.5e9, 2.0e9]
    assert vna.fetch_frequencies() == [1.0e9, 1.5e9, 2.0e9]


# --- markers ----------------------------------------------------------------

def test_marker_domain_guard_blocks_band_change(vna):
    vna.marker_set_range(1.0e9, 2.0e9)
    with pytest.raises(InstrumentError, match="marker domain already set"):
        vna.marker_set_range(3.0e9, 4.0e9)


def test_marker_domain_same_band_is_idempotent(vna, session):
    vna.marker_set_range(1.0e9, 2.0e9)
    vna.marker_set_range(1.0e9, 2.0e9)  # no error


def test_marker_domain_clear_allows_new_band(vna, session):
    vna.marker_set_range(1.0e9, 2.0e9)
    vna.marker_clear_domain()
    vna.marker_set_range(3.0e9, 4.0e9)
    assert "CALC1:MARK:FUNC:DOM OFF" in session.writes
    assert "CALC1:MARK:FUNC:DOM:STAR 3000000000" in session.writes


def test_preset_clears_marker_domain(vna):
    vna.marker_set_range(1.0e9, 2.0e9)
    vna.preset()
    vna.marker_set_range(3.0e9, 4.0e9)  # no error after preset


def test_marker_configure_orchestration(vna, session):
    spec = MarkerSpec(index=2, f_start_hz=1.0e9, f_stop_hz=2.0e9, func=MarkerFunc.MAX)
    vna.marker_configure(spec)
    assert session.writes == [
        "CALC1:MARK2:STAT ON",
        "CALC1:MARK:FUNC:DOM:STAR 1000000000",
        "CALC1:MARK:FUNC:DOM:STOP 2000000000",
        "CALC1:MARK:FUNC:DOM ON",
        "CALC1:MARK2:FUNC:TYPE MAX",
        "CALC1:MARK2:FUNC:EXEC",
    ]


def test_marker_read(vna, session):
    session.responses["CALC1:MARK1:X?"] = "1.5E9"
    session.responses["CALC1:MARK1:Y?"] = "-12.5,0.0"
    assert vna.marker_read(1) == (1.5e9, -12.5)


# --- errors & lifecycle -----------------------------------------------------

def test_get_errors_drains_queue(vna, session):
    session.responses["SYST:ERR?"] = [
        '-113,"Undefined header"',
        '-410,"Query INTERRUPTED"',
        '+0,"No error"',
    ]
    assert vna.get_errors() == ['-113,"Undefined header"', '-410,"Query INTERRUPTED"']


def test_context_manager_closes_session(session):
    with E5071C(session):
        pass
    assert session.closed is True
