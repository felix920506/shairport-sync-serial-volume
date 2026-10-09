"""Amplifier backends and the registry used to pick one from config.

``type`` in the ``[amplifier]`` config section is either a built-in name from
:data:`BUILTIN` or a ``"package.module:ClassName"`` reference to any
:class:`~volume_bridge.amplifiers.base.Amplifier` subclass on the Python path,
so new amplifiers can be added without modifying this package.
"""

from __future__ import annotations

import importlib
from typing import Any, Mapping

from .base import Amplifier, AmplifierState

BUILTIN = {
    "stub": "volume_bridge.amplifiers.stub:StubAmplifier",
    "tcp-serial": "volume_bridge.amplifiers.tcp_serial:TcpSerialAmplifier",
    "tcp-line": "volume_bridge.amplifiers.tcp_serial:TcpSerialAmplifier",  # older name
}

__all__ = ["Amplifier", "AmplifierState", "BUILTIN", "create_amplifier", "load_amplifier_class"]


def load_amplifier_class(type_name: str) -> type[Amplifier]:
    target = BUILTIN.get(type_name, type_name)
    module_name, sep, class_name = target.partition(":")
    if not sep:
        raise ValueError(
            f"unknown amplifier type {type_name!r}; use one of {sorted(BUILTIN)} or 'module:ClassName'"
        )
    cls = getattr(importlib.import_module(module_name), class_name)
    if not (isinstance(cls, type) and issubclass(cls, Amplifier)):
        raise TypeError(f"{target} is not an Amplifier subclass")
    return cls


def create_amplifier(type_name: str, options: Mapping[str, Any]) -> Amplifier:
    return load_amplifier_class(type_name)(options)
