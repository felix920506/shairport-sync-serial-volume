import asyncio
import socket

from volume_bridge.shairport import ShairportSync, _MetadataProtocol


def message(code, data=b""):
    return b"ssnc" + code + data


def test_startup_volume_is_unknown_until_a_source_sets_one():
    from dbus_fast import Variant

    source = ShairportSync()
    source._volume = 0.0  # what Shairport Sync reports at startup
    assert source.volume is None
    seen = []
    source.on_volume = seen.append
    source._properties_changed("org.gnome.ShairportSync.RemoteControl",
                               {"AirplayVolume": Variant("d", -24.0)}, [])
    assert source.volume == -24.0 and seen == [-24.0]


def test_connected_falls_back_to_active_without_metadata():
    source = ShairportSync()
    assert not source.connected
    source._active = True
    assert source.connected


def test_conn_and_disc_track_connected_sources():
    source = ShairportSync()
    source._active = True
    source._metadata_message(b"ssnc", b"pvol", b"-20.0,0,0,0")
    assert not source.connected  # metadata arrives, so "active" no longer counts

    source._metadata_message(b"ssnc", b"conn", b"fe80::1")
    assert source.connected
    source._metadata_message(b"ssnc", b"conn", b"192.168.1.9")
    source._metadata_message(b"ssnc", b"disc", b"fe80::1")
    assert source.connected  # the second source is still there
    source._metadata_message(b"ssnc", b"disc", b"192.168.1.9")
    assert not source.connected
    source._metadata_message(b"ssnc", b"disc", b"192.168.1.9")  # unmatched: ignored
    source._metadata_message(b"ssnc", b"conn", b"fe80::1")
    assert source.connected


async def test_metadata_is_received_over_udp():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    source = ShairportSync(metadata_port=port)
    source._metadata, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: _MetadataProtocol(source._metadata_message),
        local_addr=("127.0.0.1", port))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(message(b"conn", b"fe80::1"), ("127.0.0.1", port))
        for _ in range(100):
            if source.connected:
                break
            await asyncio.sleep(0.01)
        assert source.connected
    finally:
        source.close()
