"""Runs the browser save's tests (save.test.mjs) with Node or Deno, so they're
part of `make test`, CI and `make mutate`. Skipped (and shown as skipped) only
on a machine with neither; CI has Node.
"""
import os
import shutil
import subprocess

import pytest

JS = (["node"] if shutil.which("node") else
      ["deno", "run", "--quiet", "--allow-read"] if shutil.which("deno") else None)
HERE = os.path.dirname(__file__)


@pytest.mark.skipif(not JS, reason="needs node or deno")
def test_browser_save():
    r = subprocess.run([*JS, os.path.join(HERE, "save.test.mjs")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
