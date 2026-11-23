"""Run the real CLI entry point with the test's libgit2 config in this interpreter."""

import os
from pathlib import Path
from runpy import run_module

from tests.git_support import isolate_libgit2_config

with isolate_libgit2_config(Path(os.environ["GIT_CONFIG_GLOBAL"]).parent):
    run_module("git_stack", run_name="__main__", alter_sys=True)
