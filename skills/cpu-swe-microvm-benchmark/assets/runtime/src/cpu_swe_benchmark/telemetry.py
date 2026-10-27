"""Scoped host, guest, VMM and model-service time series with explicit availability."""
from __future__ import annotations

import csv
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit

from cpu_swe_benchmark.aggregate import percentile


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


class CPUReader:
    def __init__(self, pid=None):
        self.pid = pid
        self.previous = {}
        self.pid_previous = None
        self.cpuset = set(os.sched_getaffinity(0))

    def utilization(self, key, values):
        total, idle = sum(values[:8]), values[3] + values[4]
        old = self.previous.get(key)
        self.previous[key] = (total, idle)
        if old is None or total <= old[0]:
            return None
        return max(0.0, min(100.0, 100 * (1 - (idle - old[1]) / (total - old[0]))))

    def __call__(self):
        lines = Path('/proc/stat').read_text().splitlines()
        result = {'cpu_util_percent': self.utilization('all', [int(v) for v in lines[0].split()[1:]])}
        selected = []
        for line in lines[1:]:
            label, *values = line.split()
            if label.startswith('cpu') and label[3:].isdigit() and int(label[3:]) in self.cpuset:
                selected.append([int(v) for v in values])
        if selected:
            result['allocated_cpu_util_percent'] = self.utilization('allocated', [sum(v) for v in zip(*selected)])
        memory = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            memory[key] = int(value.strip().split()[0])
        total = memory['MemTotal']
        used = total - memory['MemAvailable']
        result.update(memory_total_mib=total / 1024, memory_used_mib=used / 1024, memory_used_percent=used / total * 100)
        if self.pid is not None:
            result.update(process_sample(self.pid, self))
        return result


def process_sample(pid, reader):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    ticks = int(fields[11]) + int(fields[12])
    now = time.monotonic()
    old = reader.pid_previous
    reader.pid_previous = (now, ticks)
    cpu = None if old is None else (ticks - old[1]) / os.sysconf('SC_CLK_TCK') / (now - old[0]) * 100
    return {'process_cpu_core_percent': cpu, 'process_rss_mib': int(fields[21]) * os.sysconf('SC_PAGE_SIZE') / 1024**2, 'process_threads': int(fields[17])}


class ProcessReader:
    def __init__(self, pid):
        self.pid = pid
        self.pid_previous = None
    def __call__(self):
        return process_sample(self.pid, self)


GPU_FIELDS = 'index,uuid,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,clocks.sm,clocks.mem,temperature.gpu'
GPU_KEYS = ['index', 'uuid', 'util_percent', 'memory_controller_util_percent', 'memory_used_mib', 'memory_total_mib', 'power_watts', 'sm_clock_mhz', 'memory_clock_mhz', 'temperature_celsius']


class HostReader(CPUReader):
    def __call__(self):
        result = super().__call__()
        frequencies = [number(Path(f'/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq').read_text()) for cpu in self.cpuset if Path(f'/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq').exists()]
        frequencies = [v / 1000 for v in frequencies if v is not None]
        if frequencies:
            result['allocated_cpu_frequency_mhz'] = sum(frequencies) / len(frequencies)
        gpus = []
        try:
            output = subprocess.run(['nvidia-smi', '--query-gpu=' + GPU_FIELDS, '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=3, check=True).stdout
            for row in csv.reader(output.splitlines()):
                if len(row) != len(GPU_KEYS):
                    continue
                gpu = {key: (value.strip() if key == 'uuid' else number(value)) for key, value in zip(GPU_KEYS, row)}
                gpus.append(gpu)
            for key in GPU_KEYS[2:]:
                values = [gpu[key] for gpu in gpus if gpu[key] is not None]
                if values:
                    # Power and memory capacity/usage are sums across visible GPUs.
                    total_key = key in {'power_watts', 'memory_used_mib', 'memory_total_mib'}
                    result['gpu_' + key + ('_total' if total_key else '')] = sum(values) if total_key else sum(values) / len(values)
            result['gpu_device_count'] = len(gpus)
            result['gpu_collection_status'] = 'available' if gpus else 'unavailable'
        except Exception as exc:
            result['gpu_collection_status'] = 'unavailable'
            result['gpu_collection_error'] = str(exc)
        result['gpus'] = gpus
        return result


SERVICE_METRICS = {'num_requests_running', 'num_requests_waiting', 'gpu_cache_usage_perc', 'kv_cache_usage_perc', 'prompt_tokens_total', 'generation_tokens_total', 'num_preemptions_total', 'request_success_total', 'time_to_first_token_seconds_sum', 'time_to_first_token_seconds_count', 'time_per_output_token_seconds_sum', 'time_per_output_token_seconds_count', 'e2e_request_latency_seconds_sum', 'e2e_request_latency_seconds_count'}


def parse_prometheus(text):
    values = {}
    for line in text.splitlines():
        match = re.match(r'^vllm:([a-zA-Z0-9_]+)(?:\{[^}]*\})?\s+([^\s]+)', line)
        if match and match[1] in SERVICE_METRICS:
            value = number(match[2])
            if value is not None:
                values.setdefault(match[1], []).append(value)
    return {key: sum(items) / len(items) if key.endswith('_perc') else sum(items) for key, items in values.items()}


class ServiceReader:
    def __init__(self, endpoints):
        self.urls = sorted({f'{urlsplit(endpoint).scheme}://{urlsplit(endpoint).netloc}/metrics' for endpoint in endpoints})
    def __call__(self):
        import requests
        series = []
        errors = []
        for url in self.urls:
            try:
                response = requests.get(url, timeout=2)
                response.raise_for_status()
                values = parse_prometheus(response.text)
                if not values:
                    raise ValueError('No supported vLLM metrics exposed')
                series.append({'endpoint': url, **values})
            except Exception as exc:
                errors.append({'endpoint': url, 'error': str(exc)})
        result = {'endpoints': series, 'errors': errors, 'endpoint_count': len(series)}
        for key in {key for row in series for key in row if key != 'endpoint'}:
            values = [row[key] for row in series if key in row]
            result[key] = sum(values) / len(values) if key.endswith('_perc') else sum(values)
        result['collection_status'] = 'available' if len(series) == len(self.urls) else ('partial' if series else 'unavailable')
        return result


class TimelineSampler:
    def __init__(self, path, reader, *, scope, interval=0.5, counters=False):
        self.path = Path(path)
        self.reader = reader
        self.scope = scope
        self.interval = interval
        self.counters = counters
        self.samples = []
        self.stop_event = threading.Event()
        self.thread = None
        self.result = None
    def take(self):
        try:
            row = self.reader()
        except Exception as exc:
            row = {'collection_status': 'unavailable', 'error': str(exc)}
        row = {'timestamp': time.time(), 'monotonic': time.monotonic(), 'scope': self.scope, **row}
        self.samples.append(row)
        with self.path.open('a') as stream:
            stream.write(json.dumps(row) + '\n')
    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('')
        self.take()
        def run():
            while not self.stop_event.wait(self.interval):
                self.take()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
    def stop(self):
        if self.result is not None:
            return self.result
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)
            if self.thread.is_alive():
                raise RuntimeError(f'{self.scope} collector did not stop')
        self.take()
        result = {self.scope + '_sample_count': len(self.samples), self.scope + '_samples_path': str(self.path)}
        statuses = [row.get('collection_status', 'available') for row in self.samples]
        result[self.scope + '_collection_status'] = 'available' if all(v == 'available' for v in statuses) else ('unavailable' if all(v == 'unavailable' for v in statuses) else 'partial')
        result[self.scope + '_sampled_window_seconds'] = self.samples[-1]['monotonic'] - self.samples[0]['monotonic']
        keys = {key for row in self.samples for key, value in row.items() if isinstance(value, (float, int)) and key not in {'timestamp', 'monotonic'}}
        for key in keys:
            values = [row[key] for row in self.samples if isinstance(row.get(key), (float, int))]
            if self.counters and key.endswith(('_total', '_sum', '_count')) and key != 'endpoint_count':
                delta = values[-1] - values[0]
                result[f'{self.scope}_{key}_delta'] = delta if delta >= 0 else None
            else:
                result[f'{self.scope}_{key}_avg'] = sum(values) / len(values)
                result[f'{self.scope}_{key}_p90'] = percentile(values, 90)
                result[f'{self.scope}_{key}_max'] = max(values)
        result[self.scope + '_gpu_collection_status'] = next((row['gpu_collection_status'] for row in reversed(self.samples) if 'gpu_collection_status' in row), 'not_applicable')
        self.result = result
        return result


def write_csv(path, rows):
    path = Path(path)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def export_task_tables(point_dir, runs):
    llm = []
    latency = []
    guest = []
    vmm = []
    for run in runs:
        common = {'run_id': run.run_id, 'workload': run.workload, 'concurrency': run.concurrency, 'status': run.status, 'agent_placement': run.execution_metadata.get('agent_placement', 'host')}
        for call in run.model_call_log:
            llm.append({**common, **call})
        latency.append({**common, 'legacy_agent_e2e_seconds': run.total_wall_time_seconds, 'llm_time_seconds': run.llm_time_total_seconds, 'bash_time_seconds': run.bash_time_total_seconds, 'model_calls': run.model_calls, 'bash_calls': run.bash_calls, 'prompt_tokens': run.prompt_tokens, 'completion_tokens': run.completion_tokens, **run.lifecycle_seconds})
        guest.append({**common, 'guest_boot_id': run.execution_metadata.get('guest_boot_id', ''), **{k:v for k,v in run.telemetry_metrics.items() if k.startswith('guest_')}})
        vmm.append({**common, 'vmm_pid': run.execution_metadata.get('vmm_pid', ''), **{k:v for k,v in run.telemetry_metrics.items() if k.startswith('vmm_')}})
    for name, rows in [('llm_calls.csv', llm), ('latency_metrics.csv', latency), ('guest_metrics.csv', guest), ('vmm_metrics.csv', vmm)]:
        write_csv(Path(point_dir)/name, rows)
