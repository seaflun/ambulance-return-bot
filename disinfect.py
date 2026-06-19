"""Compatibility entrypoint for the public-duty disinfection automation."""

from __future__ import annotations

import sys

from _runtime_loader import load_runtime_module


_runtime_module = load_runtime_module("disinfect")

if __name__ != "__main__":
    sys.modules[__name__] = _runtime_module
