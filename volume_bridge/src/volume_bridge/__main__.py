from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .amplifiers import create_amplifier
from .bridge import VolumeBridge
from .config import load_config
from .hooks import HookRunner
from .output import OutputWatcher, alsa_card_path
from .shairport import ShairportSync

log = logging.getLogger("volume_bridge")


async def run(config_path: str) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(config.log_level)

    amplifier = create_amplifier(config.amplifier_type, config.amplifier_options)
    source = ShairportSync(bus=config.shairport_bus, service=config.shairport_service,
                           metadata_port=config.shairport_metadata_port)
    bridge = VolumeBridge(source, amplifier, config.amplifier_scale, config.bridge)
    hooks = HookRunner(config.hooks, source, amplifier, bridge)
    source.on_volume = bridge.on_source_volume

    def on_active(active: bool) -> None:
        bridge.on_source_active(active)
        hooks.fire("session_start" if active else "session_end")

    def on_connection(connected: bool, client: str) -> None:
        hooks.fire("connect" if connected else "disconnect", client)

    source.on_active = on_active
    source.on_connection = on_connection
    watcher = create_output_watcher(config.alsa_device, source, hooks) if "output_lost" in config.hooks else None

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info("amplifier: %s, scale %s", config.amplifier_type, config.amplifier_scale)
    if config.hooks:
        log.info("hooks: %s", ", ".join(f"{e} ({len(a)} actions)" for e, a in config.hooks.items()))
    await source.connect()
    await amplifier.start()
    if watcher is not None:
        watcher.start()
    try:
        disconnected = asyncio.ensure_future(source.wait_disconnected())
        stopped = asyncio.ensure_future(stop.wait())
        done, pending = await asyncio.wait({disconnected, stopped}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if disconnected in done:
            log.error("lost connection to the D-Bus bus")
            return 1
        return 0
    finally:
        if watcher is not None:
            watcher.close()
        hooks.close()
        bridge.close()
        source.close()
        await amplifier.stop()


def create_output_watcher(device: str, source: ShairportSync, hooks: HookRunner) -> OutputWatcher | None:
    """Fire output_lost when the DAC disappears while audio is playing."""
    path = alsa_card_path(device)
    if path is None:
        log.warning("on_output_lost is disabled: no ALSA card in the output device %r "
                    "(set ALSA_DEVICE or [shairport] alsa_device, e.g. \"hw:CARD=N05XD\")", device)
        return None

    def on_change(present: bool) -> None:
        if present:
            log.info("output device %s is back", path.name)
        elif not source.playing:
            log.info("output device %s is gone (not playing: no action)", path.name)
        elif hooks.pending("output_lost"):
            log.info("output device %s is gone again (output_lost hooks still running)", path.name)
        else:
            log.info("output device %s is gone while playing", path.name)
            hooks.fire("output_lost")

    return OutputWatcher(path, on_change)


def main() -> None:
    parser = argparse.ArgumentParser(prog="volume-bridge", description=__doc__)
    parser.add_argument("--config", default="/etc/volume-bridge.toml", help="path to TOML config")
    args = parser.parse_args()
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    sys.exit(asyncio.run(run(args.config)))


if __name__ == "__main__":
    main()
