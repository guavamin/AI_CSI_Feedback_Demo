"""The control link between the two stations (separate laptops)."""

import socket
import time

import pytest

from common.station_link import LinkClient, LinkError, LinkServer


@pytest.fixture
def server():
    calls = []
    srv = LinkServer({"echo": lambda a: {"got": a}, "boom": lambda a: 1 / 0, "log": lambda a: calls.append(a)},
                     key="k1", host="127.0.0.1", port=0).start()
    srv.calls = calls
    yield srv
    srv.close()


def client(srv, key="k1", **kw):
    return LinkClient("127.0.0.1", key=key, port=srv.address[1], **kw)


def test_round_trip_and_heartbeat(server):
    c = client(server)
    assert c.call("echo", x=1) == {"got": {"x": 1}}
    assert server.connected() and c.connected()
    assert c.rtt_s < 0.5


def test_wrong_key_is_refused(server):
    with pytest.raises(LinkError, match="wrong key"):
        client(server, key="nope").call("echo")
    assert not server.connected()


def test_handler_errors_and_unknown_ops_come_back_as_link_errors(server):
    c = client(server)
    with pytest.raises(LinkError, match="division"):
        c.call("boom")
    with pytest.raises(LinkError, match="unknown op"):
        c.call("nothing")
    assert c.call("echo") == {"got": {}}  # the connection survives both


def test_delay_and_reconnect(server):
    c = client(server, delay_s=0.1)
    t0 = time.monotonic()
    c.call("echo")
    assert time.monotonic() - t0 >= 0.2  # 100 ms each way
    c._sock.shutdown(socket.SHUT_RDWR)  # the link drops (e.g. Wi-Fi roaming)
    with pytest.raises(LinkError):
        c.call("echo")
    assert c.call("echo") == {"got": {}}  # next call reconnects


def test_unreachable_station():
    with pytest.raises(LinkError):
        LinkClient("127.0.0.1", key="k", port=1, timeout_s=0.5).call("status")


def test_link_errors_are_os_errors():
    # apply_mode_scheduled treats OSError from a side as "unreachable": the switch is cancelled on both sides
    assert issubclass(LinkError, OSError)
