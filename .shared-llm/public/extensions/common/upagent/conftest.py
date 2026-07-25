"""Test-suite-only platform override for the UpAgent runtime.

The production entrypoints (``client.main`` / ``recruiter.main``) fail loud on
platforms without a process birth-identity implementation (supported: Linux
via /proc, macOS via KERN_PROC_PID/KERN_PROCARGS2 sysctl). The test suite calls those
entrypoints directly for behavior that does not depend on process identity,
so it opts out explicitly here for any other platform.
"""

import os

os.environ.setdefault("UPAGENT_ALLOW_UNSUPPORTED_PLATFORM", "1")
