import pytest

from vna_library import E5071C


class FakeSession:
    """In-memory VisaSession that records writes and scripts query replies.

    Query responses are looked up by exact command string.  A list value is
    consumed one element per call (for repeated queries like SYST:ERR?).
    Unscripted *OPC? / *ESR? / SYST:ERR? get benign defaults so happy-path
    tests don't have to script them.
    """

    _DEFAULTS = {"*OPC?": "1", "*ESR?": "0", "SYST:ERR?": '+0,"No error"'}

    def __init__(self, responses=None):
        self.writes = []
        self.responses = dict(responses or {})
        self.timeout = 30000
        self.closed = False

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        self.writes.append(cmd)
        if cmd in self.responses:
            value = self.responses[cmd]
            if isinstance(value, list):
                return value.pop(0)
            return value
        if cmd in self._DEFAULTS:
            return self._DEFAULTS[cmd]
        raise AssertionError(f"unscripted query: {cmd!r}")

    def query_ascii_values(self, cmd):
        self.writes.append(cmd)
        if cmd in self.responses:
            return self.responses[cmd]
        raise AssertionError(f"unscripted values query: {cmd!r}")

    def close(self):
        self.closed = True


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def vna(session):
    return E5071C(session)
