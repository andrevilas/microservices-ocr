"""Absolute-path stdio launcher: independent of the desktop client's cwd."""
from pathlib import Path
import runpy
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
runpy.run_module("app.mcp_stdio", run_name="__main__")
