"""Explicit integration-only disposable engine fixture; never a runtime import."""
import importlib.util
import os
from pathlib import Path
root = Path(os.environ['ATROPOS_SOURCE_ROOT']).resolve()
spec = importlib.util.spec_from_file_location('atropos_synthetic_fixture', root / 'tests' / 'support.py')
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)
