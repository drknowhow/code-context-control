"""Test-session isolation from the developer's own C3 configuration.

The problem this fixes, observed live: `c3 enforce advisory` writes
`~/.c3/config.json`, and seventeen tests across four files then failed — not
because anything was broken, but because they resolve the enforcement policy
from the ambient home directory and were written when it was `strict`. CI never
saw it (no home config on a fresh runner), so the suite was green on the server
and red on the machine of whoever had used the feature.

A test suite that depends on the developer's settings gives an answer about
their machine, not about the code. Worse, it teaches people that some failures
are normal — which is how a real regression gets waved through.

`enforcement_policy` already honours `C3_HOME` as its global scope, so pointing
that at an empty directory for the whole session isolates the policy layer
without touching a single test. Tests that manipulate `C3_HOME` themselves
still work: they set and restore around their own block, and the value they
restore to is simply this one.

Note the limit: `override_policy` deliberately reads `~` and does NOT honour
`C3_HOME` (see its docstring), so this fixture does not isolate that layer. It
covers the failures that actually occur.
"""
from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_c3_home():
    """Point C3_HOME at an empty directory for the whole session."""
    previous = os.environ.get("C3_HOME")
    with tempfile.TemporaryDirectory(prefix="c3-test-home-") as home:
        os.environ["C3_HOME"] = home
        try:
            yield home
        finally:
            if previous is None:
                os.environ.pop("C3_HOME", None)
            else:
                os.environ["C3_HOME"] = previous
