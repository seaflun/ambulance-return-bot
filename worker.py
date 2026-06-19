"""Compatibility entrypoint for the public-duty worker package."""

from __future__ import annotations

import sys

from _runtime_loader import load_runtime_module


_runtime_module = load_runtime_module("worker")

if __name__ == "__main__":
    _runtime_module.main()
else:
    sys.modules[__name__] = _runtime_module
