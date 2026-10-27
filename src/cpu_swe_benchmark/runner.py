from __future__ import annotations

import concurrent.futures
import contextlib
import csv
import multiprocessing
import numpy as np
from cpu_swe_benchmark.microvm_environment import MicroVMConfig, image_identity
from cpu_swe_benchmark.telemetry import TimelineSampler, HostReader, ServiceReader, export_task_tables, write_csv
import json
import time
from pathlib import Path

from cpu_swe_benchmark.amd_pcm import AMDuProfPcmMemorySampler
from cpu_swe_benchmark.aggregate import aggregate_runs
from cpu_swe_benchmark.dcgm_sampler import DCGMGPUSampler
from cpu_swe_benchmark.schemas import ConcurrencySummary, RunResult, to_jsonable
from cpu_swe_benchmark.system_metrics import read_system_metrics
from cpu_swe_benchmark.system_sampler import SystemSampler
from cpu_swe_benchmark.worker import WorkerConfig, run_worker
from cpu_swe_benchmark.workloads import Workload


def parse_concurrency_levels(spec: str) -> list[int]:
    try:
        levels = [int(part.strip()) for part in spec.split(",") if part.strip()]
    except ValueError as exc:
        raise ValueError(f"Invalid concurrency level list: {spec!r}") from exc
    if not levels:
        raise ValueError("At least one concurrency level is required")
    if any(level <= 0 for level in levels):
        raise ValueError("Concurrency levels must be positive")
    if len(set(levels)) != len(levels):
        raise ValueError("Concurrency levels must not contain duplicates")
    return levels


def parse_endpoints(spec: str) -> list[str]:
    endpoints = [part.strip().rstrip("/") for part in spec.split(",") if part.strip()]
    if not endpoints:
        raise ValueError("At least one endpoint is required")
    return endpoints


def endpoint_for_worker(worker_index: int, endpoints: list[str]) -> str:
    if not endpoints:
        raise ValueError("At least one endpoint is required")
    return endpoints[worker_index % len(endpoints)]


def merge_dcgm_metrics(system_metrics: dict[str, object], dcgm_metrics: dict[str, object]) -> dict[str, object]:
    merged = dict(system_metrics)
    if int(dcgm_metrics.get("dcgm_sample_count", 0) or 0) > 0:
        for key, value in dcgm_metrics.items():
            merged[key] = value
    else:
        for key in ("dcgm_sample_count", "dcgm_error", "dcgm_stdout_log", "dcgm_stderr_log"):
            if key in dcgm_metrics:
                merged[key] = dcgm_metrics[key]
    return merged


def run_concurrency_point(
    *,
    workload: Workload,
    concurrency: int,
    endpoints: list[str],
    model: str,
    api_key: str,
    output_dir: Path,
    mini_swe_agent_src: Path,
    vllm_topology: str,
    task_timeout_seconds: int,
    command_timeout_seconds: int,
    cpu_threads_per_worker: int,
    model_max_tokens: int,
    model_temperature: float,
    execution_backend: str = "native",
    microvm_config: MicroVMConfig | None = None,
    max_parallel_vm_starts: int = 4,
) -> ConcurrencySummary:
    if execution_backend not in {"native", "firecracker"}:
        raise ValueError("Unknown execution backend")
    if max_parallel_vm_starts < 1:
        raise ValueError("max_parallel_vm_starts must be positive")
    if execution_backend == "firecracker" and microvm_config is None:
        raise ValueError("microVM configuration required")
    identity = image_identity(microvm_config) if execution_backend == "firecracker" else {"agent_placement": "host"}
    point_dir = output_dir / workload.name / f"c{concurrency:03d}"
    point_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = point_dir / "runs"
    runs_dir.mkdir(exist_ok=True)
    batch_start = time.time()
    futures: list[concurrent.futures.Future[RunResult]] = []
    results: list[RunResult] = []
    sampler = SystemSampler(interval_seconds=1.0, metrics_reader=read_system_metrics)
    pcm_sampler = AMDuProfPcmMemorySampler(point_dir / "amd_pcm")
    dcgm_sampler = DCGMGPUSampler(point_dir / "dcgm")
    host_timeline = TimelineSampler(point_dir / 'host_metrics.jsonl', HostReader(), scope='host', interval=1.0)
    service_timeline = TimelineSampler(point_dir / 'vllm_service_metrics.jsonl', ServiceReader(endpoints), scope='service', interval=1.0, counters=True)
    host_timeline.start()
    service_timeline.start()
    sampler.start()
    pcm_sampler.start()
    dcgm_sampler.start()

    try:
        with contextlib.ExitStack() as stack:
            semaphore = stack.enter_context(multiprocessing.Manager()).BoundedSemaphore(max_parallel_vm_starts) if execution_backend == "firecracker" else None
            pool = stack.enter_context(concurrent.futures.ProcessPoolExecutor(max_workers=concurrency))
            for worker_index in range(concurrency):
                run_id = f"{workload.name}_c{concurrency:03d}_w{worker_index:03d}"
                config = WorkerConfig(
                    run_id=run_id,
                    worker_index=worker_index,
                    workload=workload,
                    concurrency=concurrency,
                    endpoint=endpoint_for_worker(worker_index, endpoints),
                    model=model,
                    api_key=api_key,
                    run_dir=runs_dir / run_id,
                    mini_swe_agent_src=mini_swe_agent_src,
                    task_timeout_seconds=task_timeout_seconds,
                    command_timeout_seconds=command_timeout_seconds,
                    cpu_threads_per_worker=cpu_threads_per_worker,
                    model_max_tokens=model_max_tokens,
                    model_temperature=model_temperature,
                    execution_backend=execution_backend,
                    microvm_config=microvm_config,
                    vm_start_semaphore=semaphore,
                )
                futures.append(pool.submit(run_worker, config))
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
    finally:
        batch_end = time.time()
        service_telemetry = service_timeline.stop()
        host_telemetry = host_timeline.stop()
        dcgm_metrics = dcgm_sampler.stop()
        pcm_metrics = pcm_sampler.stop()
        system_metrics = sampler.stop()
        system_metrics = merge_dcgm_metrics(system_metrics, dcgm_metrics)
        system_metrics.update(pcm_metrics)

    batch_wall = batch_end - batch_start
    results.sort(key=lambda run: run.run_id)
    summary = aggregate_runs(
        results,
        workload=workload.name,
        concurrency=concurrency,
        model=model,
        base_url=",".join(endpoints),
        vllm_topology=vllm_topology,
        batch_wall_time_seconds=batch_wall,
        waves=1,
        system_metrics=system_metrics,
    )
    summary.telemetry_metrics.update(host_telemetry)
    summary.telemetry_metrics.update(service_telemetry)
    summary.telemetry_metrics['timing_schema'] = 'task_window_excludes_collector_shutdown_v2'
    summary.telemetry_metrics['gpu_metrics_scope'] = 'benchmark_host_gpu'
    summary.telemetry_metrics.update(host_cpu_source='procfs', host_gpu_source='nvidia-smi', guest_cpu_memory_source='guest_procfs', vmm_cpu_memory_source='host_procfs_per_pid', service_metrics_source='vllm_prometheus')
    summary.telemetry_metrics['memory_bandwidth_collection_status'] = 'available' if system_metrics.get('memory_bandwidth_source') else 'unavailable'
    summary.telemetry_metrics['memory_bandwidth_source'] = system_metrics.get('memory_bandwidth_source', '')
    summary.telemetry_metrics['gpu_memory_bandwidth_util_source'] = system_metrics.get('gpu_memory_bandwidth_util_source', '')
    for key in {key for run in results for key in run.telemetry_metrics}:
        values = [run.telemetry_metrics[key] for run in results if isinstance(run.telemetry_metrics.get(key), (int,float))]
        if values:
            summary.telemetry_metrics[key + '_mean_across_tasks'] = float(np.mean(values))
    summary.telemetry_metrics['guest_telemetry_collection_status'] = 'available' if results and all(run.telemetry_metrics.get('guest_collection_status') == 'available' for run in results) else ('not_applicable' if execution_backend == 'native' else 'partial')
    summary.telemetry_metrics['vmm_telemetry_collection_status'] = 'available' if results and all(run.telemetry_metrics.get('vmm_collection_status') == 'available' for run in results) else ('not_applicable' if execution_backend == 'native' else 'partial')
    calls = [call for run in results for call in run.model_call_log]
    for source, prefix in [('duration_seconds','llm_call_latency'),('ttft_seconds','llm_ttft'),('tpot_seconds','llm_tpot')]:
        values = [call[source] for call in calls if call.get('status') == 'completed' and call.get(source) is not None]
        for p in [50,90,95,99]:
            summary.telemetry_metrics[f'{prefix}_p{p}_seconds'] = float(np.percentile(values,p)) if values else None
    summary.telemetry_metrics['llm_error_calls'] = sum(call.get('status') == 'error' for call in calls)
    summary.telemetry_metrics['llm_retry_attempts'] = sum(max(0,call.get('attempt_number',1)-1) for call in calls)
    e2e = [run.lifecycle_seconds['task_e2e_seconds'] for run in results if 'task_e2e_seconds' in run.lifecycle_seconds]
    for p in [50,90,95,99]:
        summary.telemetry_metrics[f'task_e2e_p{p}_seconds'] = float(np.percentile(e2e,p)) if e2e else None
    summary.execution_backend = execution_backend
    summary.execution_metadata = identity
    keys = sorted({key for result in results for key in result.lifecycle_seconds})
    for key in keys:
        values = [result.lifecycle_seconds[key] for result in results if key in result.lifecycle_seconds]
        summary.lifecycle_seconds[key + "_mean"] = float(np.mean(values))
        summary.lifecycle_seconds[key + "_p90"] = float(np.percentile(values, 90))
    export_task_tables(point_dir, results)
    write_csv(point_dir / 'host_gpu_timeseries.csv', [{'timestamp': row['timestamp'], 'scope': 'shared_host_gpu', **gpu} for row in host_timeline.samples for gpu in row.get('gpus', [])])
    write_csv(point_dir / 'host_telemetry_summary.csv', [host_telemetry])
    write_csv(point_dir / 'vllm_service_summary.csv', [service_telemetry])
    (point_dir / 'collection_status.json').write_text(json.dumps({key:value for key,value in summary.telemetry_metrics.items() if 'status' in key or 'source' in key or 'scope' in key}, indent=2))
    (point_dir / "summary.json").write_text(json.dumps(to_jsonable(summary), indent=2), encoding="utf-8")
    with (point_dir / "runs.jsonl").open("w", encoding="utf-8") as handle:
        for run in results:
            handle.write(json.dumps(to_jsonable(run)) + "\n")
    return summary


def write_global_csv(summaries: list[ConcurrencySummary], output_dir: Path) -> Path:
    path = output_dir / "global_summary.csv"
    headers = [
        "workload",
        "concurrency",
        "submitted_tasks",
        "successful_tasks",
        "failed_tasks",
        "success_rate",
        "batch_wall_time_seconds",
        "throughput_successful_tasks_per_sec",
        "latency_p50",
        "latency_p90",
        "latency_p95",
        "latency_p99",
        "E2E_p90_seconds",
        "TTFT_p90",
        "TPOT_p90",
        "llm_input_tokens_total",
        "llm_output_tokens_total",
        "llm_total_tokens_total",
        "successful_agent_input_tokens_total",
        "successful_agent_output_tokens_total",
        "successful_agent_total_tokens_total",
        "llm_input_tokens_per_sec",
        "llm_output_tokens_per_sec",
        "llm_total_tokens_per_sec",
        "successful_agent_total_tokens_per_sec",
        "avg_input_tokens_per_task",
        "avg_output_tokens_per_task",
        "avg_total_tokens_per_task",
        "avg_total_tokens_per_successful_agent",
        "avg_llm_time_seconds_per_task",
        "avg_bash_time_seconds_per_task",
        "avg_model_calls_per_task",
        "avg_bash_calls_per_task",
        "cpu_util_avg_percent",
        "cpu_util_p50_percent",
        "cpu_util_p90_percent",
        "cpu_util_max_percent",
        "memory_used_avg_percent",
        "memory_used_max_percent",
        "memory_bandwidth_total_p90_gbps",
        "memory_bandwidth_total_max_gbps",
        "memory_bandwidth_read_p90_gbps",
        "memory_bandwidth_read_max_gbps",
        "memory_bandwidth_write_p90_gbps",
        "memory_bandwidth_write_max_gbps",
        "gpu_util_avg_percent",
        "gpu_util_p50_percent",
        "gpu_util_p90_percent",
        "gpu_util_max_percent",
        "gpu_memory_bandwidth_util_avg_percent",
        "gpu_memory_bandwidth_util_p50_percent",
        "gpu_memory_bandwidth_util_p90_percent",
        "gpu_memory_bandwidth_util_max_percent",
        "gpu_memory_used_avg_percent",
        "gpu_memory_used_max_percent",
        "workload_cpu_util_avg_percent",
        "workload_cpu_util_p50_percent",
        "workload_cpu_util_p90_percent",
        "workload_cpu_util_max_percent",
        "workload_memory_used_avg_percent",
        "workload_memory_used_max_percent",
        "workload_gpu_util_avg_percent",
        "workload_gpu_util_p50_percent",
        "workload_gpu_util_p90_percent",
        "workload_gpu_util_max_percent",
        "workload_gpu_memory_bandwidth_util_avg_percent",
        "workload_gpu_memory_bandwidth_util_p50_percent",
        "workload_gpu_memory_bandwidth_util_p90_percent",
        "workload_gpu_memory_bandwidth_util_max_percent",
        "workload_gpu_memory_used_avg_percent",
        "workload_gpu_memory_used_max_percent",
    ]
    lines = [",".join(headers)]
    for summary in summaries:
        row = [
            summary.workload,
            str(summary.concurrency),
            str(summary.submitted_tasks),
            str(summary.successful_tasks),
            str(summary.failed_tasks),
            f"{summary.success_rate:.6f}",
            f"{summary.batch_wall_time_seconds:.6f}",
            f"{summary.throughput_successful_tasks_per_sec:.6f}",
            f"{summary.latency_seconds['p50']:.6f}",
            f"{summary.latency_seconds['p90']:.6f}",
            f"{summary.latency_seconds['p95']:.6f}",
            f"{summary.latency_seconds['p99']:.6f}",
            f"{summary.latency_seconds['p90']:.6f}",
            f"{summary.model_serving_seconds.get('ttft_p90', 0.0):.6f}",
            f"{summary.model_serving_seconds.get('tpot_p90', 0.0):.6f}",
            str(summary.llm_input_tokens_total),
            str(summary.llm_output_tokens_total),
            str(summary.llm_total_tokens_total),
            str(summary.successful_agent_input_tokens_total),
            str(summary.successful_agent_output_tokens_total),
            str(summary.successful_agent_total_tokens_total),
            f"{summary.llm_input_tokens_per_sec:.6f}",
            f"{summary.llm_output_tokens_per_sec:.6f}",
            f"{summary.llm_total_tokens_per_sec:.6f}",
            f"{summary.successful_agent_total_tokens_per_sec:.6f}",
            f"{summary.avg_input_tokens_per_task:.6f}",
            f"{summary.avg_output_tokens_per_task:.6f}",
            f"{summary.avg_total_tokens_per_task:.6f}",
            f"{summary.avg_total_tokens_per_successful_agent:.6f}",
            f"{summary.avg_llm_time_seconds_per_task:.6f}",
            f"{summary.avg_bash_time_seconds_per_task:.6f}",
            f"{summary.avg_model_calls_per_task:.6f}",
            f"{summary.avg_bash_calls_per_task:.6f}",
            f"{summary.system_metrics.get('cpu_util_avg_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('cpu_util_p50_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('cpu_util_p90_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('cpu_util_max_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('memory_used_avg_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('memory_used_max_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('memory_bandwidth_total_p90_gbps', 0.0):.6f}",
            f"{summary.system_metrics.get('memory_bandwidth_total_max_gbps', 0.0):.6f}",
            f"{summary.system_metrics.get('memory_bandwidth_read_p90_gbps', 0.0):.6f}",
            f"{summary.system_metrics.get('memory_bandwidth_read_max_gbps', 0.0):.6f}",
            f"{summary.system_metrics.get('memory_bandwidth_write_p90_gbps', 0.0):.6f}",
            f"{summary.system_metrics.get('memory_bandwidth_write_max_gbps', 0.0):.6f}",
            f"{summary.system_metrics.get('gpu_util_avg_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('gpu_util_p50_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('gpu_util_p90_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('gpu_util_max_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('gpu_memory_bandwidth_util_avg_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('gpu_memory_bandwidth_util_p50_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('gpu_memory_bandwidth_util_p90_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('gpu_memory_bandwidth_util_max_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('gpu_memory_used_avg_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('gpu_memory_used_max_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_cpu_util_avg_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_cpu_util_p50_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_cpu_util_p90_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_cpu_util_max_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_memory_used_avg_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_memory_used_max_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_gpu_util_avg_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_gpu_util_p50_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_gpu_util_p90_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_gpu_util_max_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_gpu_memory_bandwidth_util_avg_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_gpu_memory_bandwidth_util_p50_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_gpu_memory_bandwidth_util_p90_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_gpu_memory_bandwidth_util_max_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_gpu_memory_used_avg_percent', 0.0):.6f}",
            f"{summary.system_metrics.get('workload_gpu_memory_used_max_percent', 0.0):.6f}",
        ]
        lines.append(",".join(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with path.open(newline="", encoding="utf-8") as handle:
        records = list(csv.DictReader(handle))
    extra = ["execution_backend", "agent_placement", "sandbox_mode", "vcpu_per_vm", "memory_mib_per_vm", "vm_cpuset", "task_e2e_p90_seconds", "sandbox_ready_mean_seconds", "sandbox_prepare_mean_seconds", "sandbox_start_failed_tasks", "sandbox_io_failed_tasks", "workload_metrics_scope"]
    for record, summary in zip(records, summaries):
        if summary.execution_backend == "firecracker":
            for key in record:
                if key.startswith("workload_gpu_"):
                    record[key] = ""
        record.update({
            "workload_metrics_scope": "guest" if summary.execution_backend == "firecracker" else "host",
            "execution_backend": summary.execution_backend,
            "agent_placement": "guest" if summary.execution_backend == "firecracker" else "host",
            "sandbox_mode": "cold_boot" if summary.execution_backend == "firecracker" else "none",
            "vcpu_per_vm": summary.execution_metadata.get("vcpus", ""),
            "memory_mib_per_vm": summary.execution_metadata.get("memory_mib", ""),
            "vm_cpuset": summary.execution_metadata.get("cpuset", ""),
            "task_e2e_p90_seconds": summary.lifecycle_seconds.get("task_e2e_seconds_p90", ""),
            "sandbox_ready_mean_seconds": summary.lifecycle_seconds.get("sandbox_ready_seconds_mean", ""),
            "sandbox_prepare_mean_seconds": summary.lifecycle_seconds.get("sandbox_prepare_seconds_mean", ""),
            "sandbox_start_failed_tasks": sum(run.get("status") == "sandbox_start_failed" for run in summary.runs),
            "sandbox_io_failed_tasks": sum(run.get("status") == "sandbox_io_failed" for run in summary.runs),
        })
    telemetry_fields = sorted({key for summary in summaries for key in summary.telemetry_metrics} - set(headers + extra))
    for record, summary in zip(records, summaries):
        record.update(summary.telemetry_metrics)
        if summary.telemetry_metrics.get('memory_bandwidth_collection_status') == 'unavailable':
            for key in record:
                if key.startswith('memory_bandwidth_') and key.endswith('_gbps'):
                    record[key] = ''
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers + extra + telemetry_fields)
        writer.writeheader()
        writer.writerows(records)
    return path


CATEGORIZED_CSV_COLUMNS: dict[str, list[str]] = {
    "business_results": [
        "workload",
        "concurrency",
        "submitted_tasks",
        "successful_tasks",
        "failed_tasks",
        "success_rate",
        "throughput_successful_tasks_per_sec",
        "E2E_p90_seconds",
    ],
    "llm_serving": [
        "workload",
        "concurrency",
        "TTFT_p90",
        "TPOT_p90",
        "llm_input_tokens_total",
        "llm_output_tokens_total",
        "llm_total_tokens_total",
        "successful_agent_input_tokens_total",
        "successful_agent_output_tokens_total",
        "successful_agent_total_tokens_total",
        "llm_input_tokens_per_sec",
        "llm_output_tokens_per_sec",
        "llm_total_tokens_per_sec",
        "successful_agent_total_tokens_per_sec",
        "avg_input_tokens_per_task",
        "avg_output_tokens_per_task",
        "avg_total_tokens_per_task",
        "avg_total_tokens_per_successful_agent",
        "avg_llm_time_seconds_per_task",
        "avg_model_calls_per_task",
    ],
    "cpu_memory": [
        "workload",
        "concurrency",
        "cpu_util_avg_percent",
        "cpu_util_p50_percent",
        "cpu_util_p90_percent",
        "cpu_util_max_percent",
        "workload_cpu_util_avg_percent",
        "workload_cpu_util_p90_percent",
        "workload_cpu_util_max_percent",
        "memory_used_avg_percent",
        "memory_used_max_percent",
        "memory_bandwidth_total_p90_gbps",
        "memory_bandwidth_read_p90_gbps",
        "memory_bandwidth_write_p90_gbps",
    ],
    "gpu_metrics": [
        "workload",
        "concurrency",
        "gpu_util_avg_percent",
        "gpu_util_p50_percent",
        "gpu_util_p90_percent",
        "gpu_util_max_percent",
        "gpu_memory_bandwidth_util_avg_percent",
        "gpu_memory_bandwidth_util_p90_percent",
        "gpu_memory_bandwidth_util_max_percent",
        "gpu_memory_used_avg_percent",
        "gpu_memory_used_max_percent",
        "workload_gpu_util_avg_percent",
        "workload_gpu_util_p90_percent",
        "workload_gpu_memory_bandwidth_util_avg_percent",
        "workload_gpu_memory_bandwidth_util_p90_percent",
        "workload_gpu_memory_used_avg_percent",
    ],
}


def _summary_csv_value(summary: ConcurrencySummary, column: str) -> str:
    string_values = {
        "workload": summary.workload,
        "concurrency": str(summary.concurrency),
        "submitted_tasks": str(summary.submitted_tasks),
        "successful_tasks": str(summary.successful_tasks),
        "failed_tasks": str(summary.failed_tasks),
        "llm_input_tokens_total": str(summary.llm_input_tokens_total),
        "llm_output_tokens_total": str(summary.llm_output_tokens_total),
        "llm_total_tokens_total": str(summary.llm_total_tokens_total),
        "successful_agent_input_tokens_total": str(summary.successful_agent_input_tokens_total),
        "successful_agent_output_tokens_total": str(summary.successful_agent_output_tokens_total),
        "successful_agent_total_tokens_total": str(summary.successful_agent_total_tokens_total),
    }
    if summary.execution_backend == "firecracker" and column.startswith("workload_gpu_"):
        return ""
    if column in string_values:
        return string_values[column]

    direct_values = {
        "success_rate": summary.success_rate,
        "throughput_successful_tasks_per_sec": summary.throughput_successful_tasks_per_sec,
        "E2E_p90_seconds": summary.latency_seconds.get("p90", 0.0),
        "TTFT_p90": summary.model_serving_seconds.get("ttft_p90", 0.0),
        "TPOT_p90": summary.model_serving_seconds.get("tpot_p90", 0.0),
        "llm_input_tokens_per_sec": summary.llm_input_tokens_per_sec,
        "llm_output_tokens_per_sec": summary.llm_output_tokens_per_sec,
        "llm_total_tokens_per_sec": summary.llm_total_tokens_per_sec,
        "successful_agent_total_tokens_per_sec": summary.successful_agent_total_tokens_per_sec,
        "avg_input_tokens_per_task": summary.avg_input_tokens_per_task,
        "avg_output_tokens_per_task": summary.avg_output_tokens_per_task,
        "avg_total_tokens_per_task": summary.avg_total_tokens_per_task,
        "avg_total_tokens_per_successful_agent": summary.avg_total_tokens_per_successful_agent,
        "avg_llm_time_seconds_per_task": summary.avg_llm_time_seconds_per_task,
        "avg_model_calls_per_task": summary.avg_model_calls_per_task,
    }
    if column in direct_values:
        return f"{direct_values[column]:.6f}"

    return f"{summary.system_metrics.get(column, 0.0):.6f}"


def write_categorized_csvs(summaries: list[ConcurrencySummary], output_dir: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for name, columns in CATEGORIZED_CSV_COLUMNS.items():
        path = output_dir / f"{name}.csv"
        lines = [",".join(columns)]
        for summary in summaries:
            lines.append(",".join(_summary_csv_value(summary, column) for column in columns))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        prefixes = {
            "cpu_memory": ("host_cpu_", "host_allocated_", "host_memory_", "guest_", "vmm_", "memory_bandwidth_"),
            "gpu_metrics": ("host_gpu_", "gpu_metrics_scope", "gpu_memory_bandwidth_util_source"),
            "llm_serving": ("service_", "llm_call_", "llm_ttft_", "llm_tpot_", "llm_error_", "llm_retry_"),
            "business_results": ("task_e2e_", "timing_schema"),
        }[name]
        extra_fields = sorted({key for summary in summaries for key in summary.telemetry_metrics if key.startswith(prefixes)} - set(columns))
        if extra_fields:
            with path.open(newline="", encoding="utf-8") as handle:
                records = list(csv.DictReader(handle))
            for record, summary in zip(records, summaries):
                record.update({key:summary.telemetry_metrics.get(key, "") for key in extra_fields})
                if name == "cpu_memory" and summary.telemetry_metrics.get("memory_bandwidth_collection_status") == "unavailable":
                    for key in record:
                        if key.startswith("memory_bandwidth_") and key.endswith("_gbps"):
                            record[key] = ""
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns + extra_fields)
                writer.writeheader()
                writer.writerows(records)
        paths[name] = path
    return paths
