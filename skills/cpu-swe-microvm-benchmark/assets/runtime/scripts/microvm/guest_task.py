#!/usr/bin/env python3
"""Run a complete mini-swe-agent attempt inside one guest Python process."""
import json
import os
from pathlib import Path
import sys

from cpu_swe_benchmark.telemetry import TimelineSampler, CPUReader
from cpu_swe_benchmark.schemas import to_jsonable
from cpu_swe_benchmark.worker import WorkerConfig, run_worker
from cpu_swe_benchmark.workloads import get_workload


def main():
    task = json.load(sys.stdin)
    task['workload'] = get_workload(task['workload'])
    task['run_dir'] = Path('/workspace/attempt')
    task['mini_swe_agent_src'] = Path('/opt/mini-swe-agent/src')
    task['execution_backend'] = 'native'
    sampler = TimelineSampler('/workspace/attempt/guest_metrics.jsonl', CPUReader(os.getpid()), scope='guest', interval=0.5)
    sampler.start()
    try:
        result = run_worker(WorkerConfig(**task))
    finally:
        guest_metrics = sampler.stop()
    result.telemetry_metrics.update(guest_metrics)
    import minisweagent
    result.execution_backend = 'firecracker'
    result.execution_metadata.update({
        'agent_placement': 'guest',
        'agent_pid': os.getpid(),
        'model_client_pid': os.getpid(),
        'agent_version': minisweagent.__version__,
        'agent_source': minisweagent.__file__,
        'guest_kernel': os.uname().release,
        'guest_boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'guest_os_release': Path('/etc/os-release').read_text(),
        'guest_python': sys.version,
        'workload_metrics_scope': 'guest',
        'model_transport': 'guest_http_over_vsock',
    })
    result.harness_validation_log['execution_backend'] = 'firecracker'
    result.harness_validation_log['validation_pid'] = os.getpid()
    result.harness_validation_log['guest_boot_id'] = result.execution_metadata['guest_boot_id']
    Path('/workspace/attempt/run_result.json').write_text(json.dumps(to_jsonable(result), indent=2))


if __name__ == '__main__':
    main()
