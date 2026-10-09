"""TOML configuration for the bridge. See config/volume-bridge.toml for a commented example."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bridge import BridgeSettings
from .hooks import Hooks, parse_hooks
from .mapping import LevelScale


@dataclass
class Config:
    log_level: str = "INFO"
    shairport_bus: str = "system"
    shairport_service: str = "org.gnome.ShairportSync"
    #: UDP port on 127.0.0.1 where Shairport Sync sends metadata (0 = don't listen).
    shairport_metadata_port: int = 5555
    bridge: BridgeSettings = field(default_factory=BridgeSettings)
    amplifier_type: str = "stub"
    amplifier_scale: LevelScale = field(default_factory=LevelScale)
    amplifier_options: dict[str, Any] = field(default_factory=dict)
    hooks: Hooks = field(default_factory=dict)


def _pick(section: dict[str, Any], names: tuple[str, ...], where: str) -> dict[str, Any]:
    unknown = set(section) - set(names)
    if unknown:
        raise ValueError(f"unknown setting(s) in [{where}]: {', '.join(sorted(unknown))}")
    return {k: section[k] for k in names if k in section}


def parse_config(data: dict[str, Any]) -> Config:
    unknown = set(data) - {"bridge", "shairport", "amplifier", "hooks"}
    if unknown:
        raise ValueError(f"unknown section(s): {', '.join(sorted(unknown))}")

    bridge = dict(data.get("bridge", {}))
    log_level = bridge.pop("log_level", "INFO")
    shairport = _pick(data.get("shairport", {}), ("bus", "service", "metadata_port"), "shairport")

    amplifier = dict(data.get("amplifier", {}))
    amp_type = amplifier.pop("type", "stub")
    scale = _pick(amplifier.pop("scale", {}),
                  ("min_level", "max_level", "step", "limit", "exponent"), "amplifier.scale")
    options = amplifier.pop("options", {})
    if amplifier:
        raise ValueError(f"unknown setting(s) in [amplifier]: {', '.join(sorted(amplifier))}")

    return Config(
        log_level=str(log_level).upper(),
        shairport_bus=shairport.get("bus", "system"),
        shairport_service=shairport.get("service", "org.gnome.ShairportSync"),
        shairport_metadata_port=int(shairport.get("metadata_port", 5555)),
        bridge=BridgeSettings(**_pick(
            bridge, ("source_echo_window", "amplifier_echo_window", "max_echo_window", "use_amplifier_mute"), "bridge")),
        amplifier_type=amp_type,
        amplifier_scale=LevelScale(**scale),
        amplifier_options=dict(options),
        hooks=parse_hooks(data.get("hooks", {})),
    )


def load_config(path: str | Path) -> Config:
    with open(path, "rb") as f:
        return parse_config(tomllib.load(f))
