import pytest

from vna_library import (
    PNA,
    EquationSpec,
    InstrumentError,
    LimitSegment,
    LimitTable,
    LimitType,
    MarkerFunc,
    MarkerSpec,
    SweepConfig,
    TraceSpec,
)


@pytest.fixture
def pna(session):
    return PNA(session)


def make_table():
    return LimitTable.from_iter(
        [
            LimitSegment(LimitType.MAX, 1.0e9, 2.0e9, -10.0, -10.0),
            LimitSegment(LimitType.MIN, 1.0e9, 2.0e9, -60.0, -60.0),
        ]
    )


def test_define_trace_uses_meas_numbers(pna, session):
    pna.define_trace(TraceSpec(name="ignored", sparam="S21"), meas=2, window=1)
    assert session.writes == [
        'CALC1:MEAS2:DEF "S21"',
        "DISP:MEAS2:FEED 1",
        "CALC1:MEAS2:FORM MLOG",
    ]


def test_define_equation_targets_existing_measurement(pna, session):
    pna.define_equation(EquationSpec(name="AM", expression="sqrt(abs(S11)^2+abs(S21)^2)"), meas=3)
    assert session.writes == [
        'CALC1:MEAS3:EQU:DEF "sqrt(abs(S11)^2+abs(S21)^2)"',
        "CALC1:MEAS3:EQU:STAT ON",
    ]


def test_apply_sweep_matches_ena_dialect(pna, session):
    pna.apply_sweep(SweepConfig(start_hz=1e9, stop_hz=2e9, points=201, if_bw_hz=100.0, power_dbm=-5.0))
    assert session.writes == [
        "SENS1:FREQ:STAR 1000000000.0",
        "SENS1:FREQ:STOP 2000000000.0",
        "SENS1:SWE:POIN 201",
        "SENS1:BWID 100.0",
        "SENS1:AVER:STAT OFF",
        "SOUR1:POW -5.0",
    ]


# --- limits -----------------------------------------------------------------

def test_limit_payload_has_no_count_prefix(pna, session):
    pna.apply_limit_table_bulk(make_table(), meas=2)
    assert session.writes == [
        "*CLS",
        "CALC1:MEAS2:LIM:DATA:DEL",
        "CALC1:MEAS2:LIM:DATA 1,1000000000,2000000000,-10,-10,2,1000000000,2000000000,-60,-60",
        "*OPC?",
        "*ESR?",
        "CALC1:MEAS2:LIM:DISP ON",
        "CALC1:MEAS2:LIM:STAT ON",
    ]


def test_limit_error_aborts_before_enabling(pna, session):
    session.responses["*ESR?"] = "32"  # execution error bit
    session.responses["SYST:ERR?"] = ['-222,"Data out of range"', '+0,"No error"']
    with pytest.raises(InstrumentError, match="Data out of range"):
        pna.apply_limit_table_bulk(make_table())
    assert not any("LIM:DISP" in w for w in session.writes)
    assert not any("LIM:STAT" in w for w in session.writes)


def test_limits_clear_uses_delete(pna, session):
    pna.limits_clear(meas=4)
    assert session.writes == ["CALC1:MEAS4:LIM:DATA:DEL"]


def test_limit_two_measurements_are_independent(pna, session):
    pna.apply_limit_table_bulk(make_table(), meas=1, check_errors=False)
    pna.apply_limit_table_bulk(make_table(), meas=2, check_errors=False)
    data_writes = [w for w in session.writes if ":LIM:DATA 1," in w]
    assert data_writes[0].startswith("CALC1:MEAS1:")
    assert data_writes[1].startswith("CALC1:MEAS2:")


def test_apply_limit_rejects_empty_table(pna):
    with pytest.raises(ValueError, match="empty"):
        pna.apply_limit_table_bulk(LimitTable(segments=()))


def test_query_limit_table_no_count_prefix(pna, session):
    session.responses["CALC1:MEAS1:LIM:DATA?"] = [
        1.0, 1.0e9, 2.0e9, -10.0, -10.0,
        2.0, 1.0e9, 2.0e9, -60.0, -60.0,
    ]
    assert pna.query_limit_table() == [
        (1, 1.0e9, 2.0e9, -10.0, -10.0),
        (2, 1.0e9, 2.0e9, -60.0, -60.0),
    ]


def test_query_limit_table_empty(pna, session):
    session.responses["CALC1:MEAS1:LIM:DATA?"] = []
    assert pna.query_limit_table() == []


# --- sweep & data -----------------------------------------------------------

def test_single_sweep_uses_sweep_mode_single(pna, session):
    pna.single_sweep(ch=2)
    assert session.writes == ["TRIG:SOUR IMM", "SENS2:SWE:MODE SING", "*OPC?"]


def test_fetch_fdata_single_value_per_point(pna, session):
    session.responses["CALC1:MEAS1:DATA:FDATA?"] = [-3.0, -6.0, -9.0]
    assert pna.fetch_fdata() == [-3.0, -6.0, -9.0]


def test_fetch_frequencies_uses_x_values(pna, session):
    session.responses["CALC1:MEAS2:X:VAL?"] = [1.0e9, 1.5e9, 2.0e9]
    assert pna.fetch_frequencies(meas=2) == [1.0e9, 1.5e9, 2.0e9]


# --- markers ----------------------------------------------------------------

def test_marker_configure_uses_per_marker_user_range(pna, session):
    spec = MarkerSpec(index=2, f_start_hz=1.0e9, f_stop_hz=2.0e9, func=MarkerFunc.MAX)
    pna.marker_configure(spec, meas=3)
    assert session.writes == [
        "CALC1:MEAS3:MARK2:STAT ON",
        "CALC1:MEAS3:MARK2:FUNC:DOM:USER 2",
        "CALC1:MEAS3:MARK2:FUNC:DOM:USER:STAR 1000000000",
        "CALC1:MEAS3:MARK2:FUNC:DOM:USER:STOP 2000000000",
        "CALC1:MEAS3:MARK2:FUNC:EXEC MAX",
    ]


def test_markers_can_use_different_bands_without_conflict(pna, session):
    # The E5071C driver raises here (shared per-channel domain); the PNA
    # platform has independent user ranges, so both bands coexist.
    pna.marker_set_range(1, 1.0e9, 2.0e9)
    pna.marker_set_range(2, 3.0e9, 4.0e9)
    assert "CALC1:MEAS1:MARK1:FUNC:DOM:USER:STAR 1000000000" in session.writes
    assert "CALC1:MEAS1:MARK2:FUNC:DOM:USER:STAR 3000000000" in session.writes


def test_marker_set_range_rejects_reversed_band(pna):
    with pytest.raises(ValueError, match="f_stop_hz"):
        pna.marker_set_range(1, 2.0e9, 1.0e9)


def test_marker_set_range_rejects_bad_user_range(pna):
    with pytest.raises(ValueError, match="user_range"):
        pna.marker_set_range(1, 1.0e9, 2.0e9, user_range=17)


def test_marker_read(pna, session):
    session.responses["CALC1:MEAS1:MARK1:X?"] = "1.5E9"
    session.responses["CALC1:MEAS1:MARK1:Y?"] = "-12.5,0.0"
    assert pna.marker_read(1) == (1.5e9, -12.5)


# --- lifecycle ----------------------------------------------------------------

def test_context_manager_closes_session(session):
    with PNA(session):
        pass
    assert session.closed is True
