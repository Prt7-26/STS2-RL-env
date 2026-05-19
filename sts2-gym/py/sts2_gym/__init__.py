"""STS2-Gym: RL/LLM environment bridge for Slay the Spire 2.

Pre-MVP / Day 3 milestone — currently only the HTTP-bridge client is
exposed. Future versions will add the gymnasium.Env wrapper, scenario
injector API, action codec, and LLM-facing observation renderers.

Quick start (assumes STS2 is running with the mod loaded):

    from sts2_gym import ModBridgeClient

    c = ModBridgeClient()
    c.wait_until_ready()
    print(c.health())
    print(c.observe()["phase"])
"""
from __future__ import annotations

from sts2_gym.client import DEFAULT_PORT, ModBridgeClient

__all__ = ["ModBridgeClient", "DEFAULT_PORT"]
__version__ = "0.0.1"
