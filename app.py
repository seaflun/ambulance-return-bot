"""Compatibility entrypoint for the public-duty runtime package."""

from __future__ import annotations

import sys

from _runtime_loader import load_runtime_module


_runtime_module = load_runtime_module("app")

if __name__ == "__main__":
    _runtime_module.run_web_app()
else:
    sys.modules[__name__] = _runtime_module
