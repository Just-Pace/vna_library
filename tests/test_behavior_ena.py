"""Behavior-level tests: the E5071C driver against a stateful ENA simulator.

These catch semantic bugs that command-string assertions cannot — which
trace a limit table lands on, trigger-source requirements, the shared
per-channel marker domain, and error-queue handling.
"""
import pytest

from vna_library import (
    E5071C,
    EquationSpec,
    InstrumentError,
    LimitSegment,
    LimitTable,
    LimitType,
    MarkerFunc,
    MarkerSpec,
    TraceSpec,
)
from tests.simulators import SimulatedE5071C


@pytest.fixture
def sim():
    return SimulatedE5071C(f_start=1.0e9, f_stop=2.0e9, points=11)


@pytest.fixture
def vna(sim):
    return E5071C(sim)


def table(lo, hi, kind=LimitType.MAX, y=-10.0):
    return LimitTable.from_iter([LimitSegment(kind, lo, hi, y, y)])


def setup_two_traces(vna):
    vna.define_trace(TraceSpec(name="Tr1", sparam="S11"), trace_num=1)
    vna.define_trace(TraceSpec(name="Tr2", sparam="S21"), trace_num=2)


def test_limits_land_on_the_named_trace(vna, sim):
    """Each trace keeps its own table — the 'only one was set' regression."""
    setup_two_traces(vna)
    vna.apply_limit_table_bulk(table(1.1e9, 1.2e9, y=-10.0), meas_name="Tr1")
    vna.apply_limit_table_bulk(table(1.5e9, 1.6e9, y=-60.0), meas_name="Tr2")
    assert sim.limits[(1, "Tr1")][3] == -10.0
    assert sim.limits[(1, "Tr2")][3] == -60.0
    # And both were enabled for display + test (the 'not set on test' regression).
    assert sim.limit_disp[(1, "Tr1")] and sim.limit_disp[(1, "Tr2")]
    assert sim.limit_test[(1, "Tr1")] and sim.limit_test[(1, "Tr2")]


def test_limit_roundtrip_through_wire_format(vna):
    setup_two_traces(vna)
    vna.apply_limit_table_bulk(table(1.1e9, 1.2e9), meas_name="Tr1")
    assert vna.query_limit_table(meas_name="Tr1") == [(1, 1.1e9, 1.2e9, -10.0, -10.0)]
    assert vna.query_limit_table(meas_name="Tr2") == []


def test_stale_instrument_error_does_not_abort_load(vna, sim):
    """A queued error from an earlier bad command (e.g. the historical
    CALC:LIM:DEL:ALL) must not make the next limit load appear to fail."""
    setup_two_traces(vna)
    sim.write("CALC1:LIM:DEL:ALL")  # the old bug: -113 Undefined header
    assert sim.error_queue
    vna.apply_limit_table_bulk(table(1.1e9, 1.2e9), meas_name="Tr1")  # must not raise
    assert sim.limit_test[(1, "Tr1")] is True


def test_limit_load_without_selection_raises(vna):
    with pytest.raises(InstrumentError):
        vna.apply_limit_table_bulk(table(1.1e9, 1.2e9))  # nothing selected


def test_selecting_unknown_trace_surfaces_error(vna, sim):
    setup_two_traces(vna)
    with pytest.raises(InstrumentError):
        vna.apply_limit_table_bulk(table(1.1e9, 1.2e9), meas_name="Nope")


def test_single_sweep_runs_on_simulator(vna, sim):
    vna.single_sweep()
    assert sim.sweeps_run == 1
    assert not sim.error_queue


def test_old_trg_pattern_fails_without_bus_source(sim):
    """Documents why the pre-fix single_sweep (*TRG with INT source) broke."""
    sim.write("INIT1:CONT OFF")
    sim.write("*TRG")
    assert sim.sweeps_run == 0
    assert any("-211" in e for e in sim.error_queue)


def test_equation_applies_to_selected_trace(vna, sim):
    setup_two_traces(vna)
    vna.define_equation(EquationSpec(name="eq", expression="S21/(1-S11)"), meas_name="Tr2")
    assert sim.equations[(1, "Tr2")] == "S21/(1-S11)"
    assert sim.eq_state[(1, "Tr2")] is True


def test_invalid_equation_raises_and_stays_off(vna, sim):
    setup_two_traces(vna)
    with pytest.raises(InstrumentError, match="equation rejected"):
        vna.define_equation(EquationSpec(name="eq", expression="sqrt(S11"), meas_name="Tr1")
    assert sim.eq_state.get((1, "Tr1")) is not True


def test_marker_search_respects_channel_domain(vna, sim):
    setup_two_traces(vna)
    vna.select_trace("Tr1")
    spec = MarkerSpec(index=1, f_start_hz=1.2e9, f_stop_hz=1.8e9, func=MarkerFunc.MIN)
    vna.marker_configure(spec)
    x, _ = vna.marker_read(1)
    assert x == 1.2e9  # constrained to the domain, not full span (1.0 GHz)


def test_channel_domain_is_shared_hence_guarded(vna):
    vna.marker_set_range(1.2e9, 1.8e9)
    with pytest.raises(InstrumentError, match="already set"):
        vna.marker_set_range(1.3e9, 1.9e9)
    vna.preset()
    vna.marker_set_range(1.3e9, 1.9e9)  # fine after preset


def test_marker_index_out_of_ena_range_rejected(vna):
    with pytest.raises(ValueError, match="1-10"):
        vna.marker_enable(11)


def test_clear_traces_resets_channel_to_one_trace(vna, sim):
    setup_two_traces(vna)
    vna.clear_traces()
    assert list(sim.traces[1]) == ["Tr1"]
    assert not sim.error_queue  # PAR:COUN is a real ENA command; no -113
