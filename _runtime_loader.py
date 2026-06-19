"""Load runtime modules from the public-duty package.

The repository root is kept for tests, release scripts, and compatibility
launchers. The public-duty runtime source lives under
``WinPython_公務電腦使用包``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parent
PUBLIC_DUTY_PACKAGE = PROJECT_ROOT / "WinPython_公務電腦使用包"


def ensure_runtime_path() -> Path:
    if not PUBLIC_DUTY_PACKAGE.exists():
        raise RuntimeError(f"Missing public-duty package: {PUBLIC_DUTY_PACKAGE}")
    package_path = str(PUBLIC_DUTY_PACKAGE)
    if package_path not in sys.path:
        sys.path.insert(0, package_path)
    return PUBLIC_DUTY_PACKAGE


def load_runtime_module(module_name: str) -> ModuleType:
    package_dir = ensure_runtime_path()
    module_path = package_dir / f"{module_name}.py"
    if not module_path.exists():
        raise RuntimeError(f"Missing runtime module: {module_path}")
    alias = f"_ambulance_public_duty_{module_name}"
    if alias in sys.modules:
        return sys.modules[alias]
    spec = importlib.util.spec_from_file_location(alias, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load runtime module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module
