"""Test-suite-only platform override for the UpAgent runtime.

The production entrypoints (``client.main`` / ``recruiter.main``) fail loud on
platforms without a process birth-identity implementation (supported: Linux
via /proc, macOS via KERN_PROC_PID/KERN_PROCARGS2 sysctl). The test suite calls those
entrypoints directly for behavior that does not depend on process identity,
so it opts out explicitly here for any other platform.
"""

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("UPAGENT_ALLOW_UNSUPPORTED_PLATFORM", "1")


@pytest.fixture(scope="session", autouse=True)
def _generated_standard_offering_roster() -> Iterator[None]:
    """Materialize the runtime build product for source-tree integration tests."""
    here = Path(__file__).resolve().parent
    source = here / "offerings.py"
    spec = importlib.util.spec_from_file_location("upagent_test_offerings", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    target = here / "offerings.yaml"
    original = target.read_bytes() if target.exists() else None
    target.write_text(module.render_roster(["standard"]))
    try:
        yield
    finally:
        if original is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(original)
