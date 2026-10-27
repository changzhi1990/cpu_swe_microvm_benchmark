#!/usr/bin/env python3
"""Repository entrypoint for the portable customer benchmark workflow."""
from pathlib import Path
import runpy

if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).resolve().parent / 'skills/cpu-swe-microvm-benchmark/scripts/benchmark.py'), run_name='__main__')
