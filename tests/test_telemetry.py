import json
import os
from pathlib import Path
import subprocess
import sys
import time

from cpu_swe_benchmark.telemetry import CPUReader, ProcessReader, TimelineSampler, parse_prometheus


def test_prometheus_aggregates_engines_and_preserves_fraction_units():
    result = parse_prometheus('''vllm:prompt_tokens_total{engine="0"} 100
vllm:prompt_tokens_total{engine="1"} 200
vllm:kv_cache_usage_perc{engine="0"} 0.2
vllm:kv_cache_usage_perc{engine="1"} 0.4
vllm:num_requests_running{engine="0"} 1
vllm:num_requests_running{engine="1"} 2
vllm:irrelevant_bucket{le="10"} 99
vllm:generation_tokens_total NaN
''')
    assert result['prompt_tokens_total'] == 300
    assert abs(result['kv_cache_usage_perc'] - 0.3) < 1e-12
    assert result['num_requests_running'] == 3
    assert 'irrelevant_bucket' not in result
    assert 'generation_tokens_total' not in result


def test_missing_metrics_are_not_manufactured_as_zeros(tmp_path):
    sampler = TimelineSampler(tmp_path/'guest.jsonl', lambda:{'collection_status':'unavailable','error':'missing collector'}, scope='guest')
    sampler.start()
    result = sampler.stop()
    assert result['guest_collection_status'] == 'unavailable'
    assert 'guest_cpu_util_percent_avg' not in result
    rows=[json.loads(line) for line in sampler.path.read_text().splitlines()]
    assert len(rows)>=2 and all(row['scope']=='guest' for row in rows)


def test_counters_use_window_delta_and_report_resets(tmp_path):
    values=iter([{'prompt_tokens_total':100,'num_requests_running':0},{'prompt_tokens_total':130,'num_requests_running':2}])
    sampler=TimelineSampler(tmp_path/'service.jsonl',lambda:next(values),scope='service',interval=60,counters=True)
    sampler.start();result=sampler.stop()
    assert result['service_prompt_tokens_total_delta']==30
    assert result['service_num_requests_running_max']==2
    reset=iter([{'generation_tokens_total':20},{'generation_tokens_total':3}])
    sampler=TimelineSampler(tmp_path/'reset.jsonl',lambda:next(reset),scope='service',interval=60,counters=True)
    sampler.start();assert sampler.stop()['service_generation_tokens_total_delta'] is None


def test_process_sampler_collects_real_cpu_and_memory(tmp_path):
    process=subprocess.Popen([sys.executable,'-c','import time; a=bytearray(12*1024*1024); stop=time.monotonic()+4\nwhile time.monotonic()<stop: pass'])
    sampler=TimelineSampler(tmp_path/'vmm.jsonl',ProcessReader(process.pid),scope='vmm',interval=0.05)
    try:
        sampler.start();time.sleep(0.25);result=sampler.stop()
        assert result['vmm_process_rss_mib_max']>8
        assert result['vmm_process_cpu_core_percent_max']>0
        assert result['vmm_sample_count']>=3
    finally:
        process.terminate();process.wait()


def test_guest_cpu_scope_reads_total_memory_and_agent_process():
    reader=CPUReader(os.getpid())
    first=reader();time.sleep(0.01);second=reader()
    assert first['cpu_util_percent'] is None
    assert 0<=second['cpu_util_percent']<=100
    assert second['memory_total_mib']>0
    assert second['process_rss_mib']>0


def test_aggregation_accepts_unavailable_then_available_collector(tmp_path):
    from cpu_swe_benchmark.standard_metrics import _aggregate_source_csv
    from cpu_swe_benchmark.telemetry import write_csv
    rows=[]
    for index in range(2):
        point=tmp_path/str(index);point.mkdir()
        write_csv(point/'global_summary.csv',[{'workload':'sorting','concurrency':str(index+1)}])
        data={'concurrency':str(index+1),'service_collection_status':'unavailable' if index==0 else 'available'}
        if index==1:data['service_prompt_tokens_total_delta']=10
        write_csv(point/'llm_serving.csv',[data])
        rows.append({'run_dir':str(point)})
    output=_aggregate_source_csv(manifest_rows=rows,source_name='llm_serving.csv',output_path=tmp_path/'merged.csv')
    import csv
    records=list(csv.DictReader(output.open()))
    assert records[0]['service_prompt_tokens_total_delta']==''
    assert records[1]['service_prompt_tokens_total_delta']=='10'
