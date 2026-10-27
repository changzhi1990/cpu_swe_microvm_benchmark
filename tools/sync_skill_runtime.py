#!/usr/bin/env python3
"""Mirror canonical runtime sources into the independently distributable skill."""
import argparse
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'skills/cpu-swe-microvm-benchmark/assets/runtime'
DIRECTORIES=['src','repo_templates','vendor','scripts/microvm']
FILES=['benchmark_latency.py','pyproject.toml']


def sources():
    result={Path(name):ROOT/name for name in FILES}
    for name in DIRECTORIES:
        for path in (ROOT/name).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':result[path.relative_to(ROOT)]=path
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check',action='store_true');args=parser.parse_args()
    expected=sources();actual={p.relative_to(DEST):p for p in DEST.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
    changed=[str(name) for name,p in expected.items() if name not in actual or p.read_bytes()!=actual[name].read_bytes()]
    stale=sorted(set(actual)-set(expected))
    if args.check:
        if changed or stale:
            print('Runtime snapshot is stale:',*changed,*(str(n) for n in stale),sep='\n');return 1
        print('Skill runtime matches canonical source:',len(expected),'files');return 0
    assert DEST.resolve().is_relative_to((ROOT/'skills/cpu-swe-microvm-benchmark/assets').resolve())
    for name in stale:actual[name].unlink()
    for name,path in expected.items():
        target=DEST/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
    print('Synchronized',len(expected),'runtime files');return 0


if __name__=='__main__':raise SystemExit(main())
