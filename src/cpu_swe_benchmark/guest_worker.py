"""Host orchestration for one complete mini-swe-agent per Firecracker guest."""
from __future__ import annotations

import json
from pathlib import Path
import time
import traceback

from cpu_swe_benchmark.microvm_environment import MicroVMEnvironment
from cpu_swe_benchmark.schemas import RunResult, to_jsonable


def run_guest_worker(config) -> RunResult:
    started = time.monotonic()
    config.run_dir.mkdir(parents=True, exist_ok=True)
    seed = config.run_dir / 'seed'
    seed.mkdir(exist_ok=True)
    vm = None
    phase = 'provision'
    lifecycle = {}
    result = None
    error = None
    status = 'sandbox_start_failed'
    exported = config.run_dir / 'guest_artifacts'
    try:
        if config.task_timeout_seconds <= 0:
            raise ValueError("Full-agent guest execution requires a positive task timeout")
        if config.microvm_config is None:
            raise ValueError('microVM configuration required')
        vm = MicroVMEnvironment(config.microvm_config, run_dir=config.run_dir, timeout=config.command_timeout_seconds, env={})
        waiting = time.monotonic()
        if config.vm_start_semaphore is not None:
            config.vm_start_semaphore.acquire()
        lifecycle['provision_queue_seconds'] = time.monotonic() - waiting
        try:
            vm.start(seed, model_endpoint=config.endpoint)
        finally:
            if config.vm_start_semaphore is not None:
                config.vm_start_semaphore.release()
        if 'full_agent' not in vm.rpc({'op': 'ping'}, timeout=5).get('capabilities', []):
            raise RuntimeError('Guest image lacks complete-agent runtime; rebuild rootfs using current build_guest.sh')
        phase = 'guest_agent'
        task = {
            'run_id': config.run_id, 'worker_index': config.worker_index,
            'workload': config.workload.name, 'concurrency': config.concurrency,
            'endpoint': vm.guest_model_endpoint, 'model': config.model, 'api_key': config.api_key,
            'task_timeout_seconds': config.task_timeout_seconds,
            'command_timeout_seconds': config.command_timeout_seconds,
            'cpu_threads_per_worker': config.cpu_threads_per_worker,
            'model_max_tokens': config.model_max_tokens, 'model_temperature': config.model_temperature,
        }
        hard_limit = config.task_timeout_seconds + config.command_timeout_seconds + 30
        response = vm.rpc({'op': 'run_task', 'config': task, 'timeout': hard_limit}, timeout=hard_limit + 10)
        lifecycle['task_e2e_seconds'] = time.monotonic() - started
        if 'run_result' in response:
            result = RunResult(**response['run_result'])
            metadata = result.execution_metadata
            if metadata.get('agent_placement') != 'guest' or not metadata.get('guest_boot_id') or not metadata.get('agent_pid'):
                raise RuntimeError('Missing guest agent provenance')
            if result.harness_validation_log.get('status') == 'passed' and result.harness_validation_log.get('guest_boot_id') != metadata['guest_boot_id']:
                raise RuntimeError('Agent and validation guest identities differ')
        else:
            status = 'timeout' if response.get('timed_out') else 'guest_agent_failed'
            error = response.get('task_error', 'Guest agent returned no result')
        phase = 'export'
        vm.export_workspace(exported)
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        status = 'sandbox_start_failed' if phase == 'provision' else 'sandbox_io_failed'
        (config.run_dir / 'exception.txt').write_text(traceback.format_exc())
        if result is not None:
            result.status = status
            result.error = error
            result.validation_passed = False
    finally:
        if vm is not None:
            vm.close()
    lifecycle.setdefault('task_e2e_seconds', time.monotonic() - started)
    if result is None:
        result = RunResult(run_id=config.run_id, workload=config.workload.name, concurrency=config.concurrency, status=status, exit_status='', total_wall_time_seconds=0.0, llm_time_total_seconds=0.0, bash_time_total_seconds=0.0, model_calls=0, bash_calls=0, validation_passed=False, endpoint=config.endpoint, trajectory_path=None, error=error, task_timeout_seconds=config.task_timeout_seconds, command_timeout_seconds=config.command_timeout_seconds)
    else:
        result.lifecycle_seconds['guest_task_e2e_seconds'] = result.lifecycle_seconds.get('task_e2e_seconds', 0.0)
    result.lifecycle_seconds.update(lifecycle)
    if vm is not None:
        result.execution_metadata.update(vm.metadata)
        result.telemetry_metrics.update({k:v for k,v in vm.metadata.items() if k.startswith('vmm_') and k != 'vmm_pid'})
        for key in ('sandbox_ready_seconds', 'sandbox_prepare_seconds', 'artifact_export_seconds', 'sandbox_destroy_seconds'):
            if key in vm.metadata:
                result.lifecycle_seconds[key] = vm.metadata[key]
    result.execution_backend = 'firecracker'
    result.execution_metadata.update({'agent_placement': 'guest', 'host_model_endpoint': config.endpoint})
    result.endpoint = config.endpoint
    if 'guest_samples_path' in result.telemetry_metrics:
        result.telemetry_metrics['guest_samples_path'] = str(exported / 'attempt/guest_metrics.jsonl')
    result.workspace = str(exported / 'attempt/workspace')
    trajectory = exported / 'attempt/trajectory.json'
    result.trajectory_path = str(trajectory) if trajectory.exists() else None
    (config.run_dir / 'run_result.json').write_text(json.dumps(to_jsonable(result), indent=2))
    return result
