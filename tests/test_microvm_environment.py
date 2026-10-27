import base64
import importlib.util
import io
from pathlib import Path
import tarfile
import time

import pytest

from cpu_swe_benchmark.microvm_environment import MicroVMConfig, extract_workspace
from cpu_swe_benchmark.validation import run_harness_validation


def archive(name, content=b'hello', kind=tarfile.REGTYPE):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as handle:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = '/etc/passwd' if kind == tarfile.SYMTYPE else ''
        info.size = len(content) if kind == tarfile.REGTYPE else 0
        handle.addfile(info, io.BytesIO(content) if kind == tarfile.REGTYPE else None)
    return base64.b64encode(output.getvalue()).decode()


@pytest.mark.parametrize('name,kind', [('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE), ('link', tarfile.LNKTYPE)])
def test_artifact_restore_rejects_paths_and_links(tmp_path, name, kind):
    with pytest.raises(ValueError):
        extract_workspace(archive(name, kind=kind), tmp_path / 'output')
    assert not (tmp_path / 'escape').exists()


def test_artifact_restore_preserves_contents(tmp_path):
    extract_workspace(archive('src/fix.py', b'print(42)\n'), tmp_path)
    assert (tmp_path / 'src/fix.py').read_text() == 'print(42)\n'


def test_validation_executes_in_guest_without_host_workspace(tmp_path):
    class Guest:
        def execute_raw(self, command, **kwargs):
            assert command == 'PYTHONPATH=src python3 -m pytest tests/test_sorting.py'
            return {'returncode': 1, 'output': 'guest test failed', 'exception_info': ''}
    result = run_harness_validation('algorithm_lab_sorting_bugfix', tmp_path / 'does-not-exist', timeout_seconds=2, python_executable='python3', environment=Guest())
    assert result['status'] == 'failed'
    assert result['output'] == 'guest test failed'


def test_validation_preserves_guest_timeout(tmp_path):
    class Guest:
        def execute_raw(self, *args, **kwargs):
            return {'returncode': -1, 'output': 'partial', 'timed_out': True}
    assert run_harness_validation('algorithm_lab_sorting_bugfix', tmp_path, timeout_seconds=1, environment=Guest())['status'] == 'timeout'


def test_guest_exec_kills_descendants_on_timeout(tmp_path, monkeypatch):
    source = Path(__file__).parents[1] / 'scripts/microvm/guest_service.py'
    spec = importlib.util.spec_from_file_location('guest_service', source)
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    monkeypatch.setattr(service, 'WORKSPACE', tmp_path)
    result = service.dispatch({'op': 'exec', 'command': '(sleep 1; touch escaped) & wait', 'timeout': 0.1})
    assert result['timed_out']
    time.sleep(1.1)
    assert not (tmp_path / 'escaped').exists()


def test_microvm_rejects_invalid_resources():
    with pytest.raises(ValueError):
        MicroVMConfig('kernel', 'rootfs', vcpus=0).validate()


def test_backend_columns_survive_standard_aggregation(tmp_path):
    import csv
    from cpu_swe_benchmark.standard_metrics import aggregate_standard_metrics
    def write(path, row):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
    point = tmp_path / 'point'
    runset = tmp_path / 'runset'
    write(point/'global_summary.csv', {'workload':'sorting', 'concurrency':'2', 'execution_backend':'firecracker', 'vm_cpuset':'8-11,16-19', 'task_e2e_p90_seconds':'3.5', 'sandbox_start_failed_tasks':'1'})
    for name in ['cpu_memory.csv','gpu_metrics.csv','llm_serving.csv']:
        write(point/name, {'concurrency':'2'})
    write(runset/'manifest.csv', {'status':'completed','concurrency':'2','run_dir':str(point)})
    outputs = aggregate_standard_metrics(runset)
    for path in [outputs.global_metrics, outputs.cpu_metrics, outputs.gpu_metrics, outputs.vllm_metrics]:
        row = next(csv.DictReader(path.open()))
        assert row['execution_backend'] == 'firecracker'
        assert row['vm_cpuset'] == '8-11,16-19'
        assert row['task_e2e_p90_seconds'] == '3.5'
        assert row['sandbox_start_failed_tasks'] == '1'
    assert next(csv.DictReader(outputs.cpu_metrics.open()))['agent_eff_freq_avg_mhz'] == ''


def test_firecracker_dispatch_does_not_start_agent_on_host(monkeypatch):
    from types import SimpleNamespace
    import cpu_swe_benchmark.guest_worker as guest
    import cpu_swe_benchmark.worker as worker
    expected = object()
    monkeypatch.setattr(guest, 'run_guest_worker', lambda config: expected)
    def forbidden(*args, **kwargs):
        raise AssertionError('Host must not instantiate the agent/model client')
    monkeypatch.setattr(worker, '_ensure_mini_swe_agent_path', forbidden)
    monkeypatch.setattr(worker, 'VLLMTextModel', forbidden)
    assert worker.run_worker(SimpleNamespace(execution_backend='firecracker')) is expected


def test_complete_guest_attempt_timeout_and_single_attempt(tmp_path, monkeypatch):
    import subprocess
    import sys
    source = Path(__file__).parents[1] / 'scripts/microvm/guest_service.py'
    spec = importlib.util.spec_from_file_location('guest_full_task_test', source)
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    monkeypatch.setattr(service, 'WORKSPACE', tmp_path)
    original = subprocess.Popen
    children = []
    def launch(argv, **kwargs):
        assert argv[-1] == '/opt/cpu-swe/guest_task.py'
        child = original([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(service.subprocess, 'Popen', launch)
    started = time.monotonic()
    result = service.dispatch({'op':'run_task', 'config':{}, 'timeout':0.1})
    assert result['timed_out']
    assert time.monotonic() - started < 2
    assert children[0].poll() is not None
    with pytest.raises(RuntimeError, match='one complete agent attempt'):
        service.dispatch({'op':'run_task', 'config':{}, 'timeout':0.1})


def test_model_serialization_does_not_persist_api_key():
    from cpu_swe_benchmark.vllm_text_model import VLLMTextModel
    model = VLLMTextModel(base_url='http://localhost/v1', model_name='test', api_key='test-transient-secret')
    assert model.serialize()['info']['config']['model']['api_key'] == '[redacted]'
