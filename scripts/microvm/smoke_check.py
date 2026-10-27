"""Run real dual-VM isolation, command, export and cleanup checks."""
import argparse
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'src'))
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from cpu_swe_benchmark.microvm_environment import MicroVMConfig, MicroVMEnvironment

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--kernel', required=True)
parser.add_argument('--rootfs', required=True)
parser.add_argument('--output-dir', required=True)
parser.add_argument('--vm-cpuset', default='')
args = parser.parse_args()
root = Path(args.output_dir).resolve()
root.mkdir(parents=True, exist_ok=True)
config = MicroVMConfig(str(Path(args.kernel).resolve()), str(Path(args.rootfs).resolve()), cpuset=args.vm_cpuset)

def check(index):
    seed = root / f'check-seed-{index}'
    seed.mkdir(exist_ok=True)
    (seed/'seed.txt').write_text(str(index))
    env = MicroVMEnvironment(config, run_dir=root/f'vm-check-{index}', timeout=5, env={})
    try:
        env.start(seed)
        result = env.execute_raw("cat /etc/os-release; python3 -c 'import sys,pytest,numpy; print(sys.version,pytest.__version__,numpy.__version__)'; uname -r")
        assert result['returncode'] == 0, result
        assert 'ID=ubuntu' in result['output'], result
        assert env.execute_raw('cat seed.txt')['output'].strip() == str(index)
        assert env.execute_raw(f'echo {index} > state.txt')['returncode'] == 0
        assert env.execute_raw('cat state.txt')['output'].strip() == str(index)
        assert env.execute_raw('exit 7')['returncode'] == 7
        result_timeout = env.execute_raw('(sleep 2; touch orphan) & wait', timeout=1)
        assert result_timeout['timed_out'], result_timeout
        assert env.execute_raw('sleep 2; test ! -e orphan')['returncode'] == 0
        assert env.execute_raw('test ! -e ' + __import__('shlex').quote(str(seed)))['returncode'] == 0
        env.export_workspace(root/f'vm-check-{index}/export')
        assert (root/f'vm-check-{index}/export/state.txt').read_text().strip() == str(index)
        return {'index':index, 'guest':result['output'], 'metadata': env.metadata, 'checks':'passed'}
    finally:
        env.close()
        assert env.process is None or env.process.poll() is not None

with ThreadPoolExecutor(max_workers=2) as pool:
    checks = list(pool.map(check, range(2)))
(root/'vm-check.json').write_text(json.dumps(checks,indent=2))
print(json.dumps(checks,indent=2))
