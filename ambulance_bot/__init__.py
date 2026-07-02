"""Compatibility package that points to the public-duty runtime source."""

from __future__ import annotations

from pathlib import Path


_PUBLIC_DUTY_PACKAGE = Path(__file__).resolve().parents[1] / "WinPython_公務電腦使用包" / "ambulance_bot"
if not _PUBLIC_DUTY_PACKAGE.exists():
    raise RuntimeError(f"Missing public-duty ambulance_bot package: {_PUBLIC_DUTY_PACKAGE}")

__path__ = [str(_PUBLIC_DUTY_PACKAGE)]
