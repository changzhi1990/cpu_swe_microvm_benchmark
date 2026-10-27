#!/usr/bin/env python3
"""Validate a completed full-agent run and create new scoped result tables."""
import argparse
import csv
import json
from pathlib import Path
import sys


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('run_dir',type=Path)
    p.add_argument('--bench-root',required=True,type=Path)
    p.add_argument('--output-dir',type=Path)
    p.add_argument('--expected-memory-mib',type=int)
    p.add_argument('--concurrency-levels')
    p.add_argument('--workload')
    args=p.parse_args(argv)
    root=args.bench_root.resolve();run=args.run_dir.resolve();out=(args.output_dir or run/'validated').resolve()
    if out.exists():raise ValueError('Refusing to overwrite an existing summary directory: '+str(out))
    sys.path.insert(0,str(root/'src'))
    from cpu_swe_benchmark.runner import write_global_csv,write_categorized_csvs
    from cpu_swe_benchmark.schemas import ConcurrencySummary
    from cpu_swe_benchmark.standard_metrics import aggregate_standard_metrics
    from cpu_swe_benchmark.telemetry import write_csv
    from cpu_swe_benchmark.workloads import parse_workload_list
    if not (run/'exit_code.txt').is_file() or (run/'exit_code.txt').read_text().strip()!='0':
        raise ValueError('No successful benchmark-process completion record; inspect benchmark.log')
    profile=json.loads((run/'run_profile.json').read_text()) if (run/'run_profile.json').is_file() else {}
    points=[int(v) for v in args.concurrency_levels.split(',')] if args.concurrency_levels else profile.get('concurrency_levels')
    workload=args.workload or profile.get('workload')
    if not points or not workload:raise ValueError('Expected workload/point set missing; provide them explicitly')
    workloads=[w.name for w in parse_workload_list(workload)]
    expected={(name,int(c)) for name in workloads for c in points}
    memory=args.expected_memory_mib
    if memory is None:memory=profile.get('memory_mib',profile.get('memory_mib_per_vm'))
    source=list(run.glob('*/c*/summary.json'));summaries=[];found=set();proof=[];issues=[];boot_ids=[]
    for path in sorted(source):
        summary=ConcurrencySummary(**json.loads(path.read_text()));identity=(summary.workload,summary.concurrency)
        if identity not in expected or identity in found:raise ValueError('Unexpected or duplicate point: '+str(identity))
        found.add(identity)
        if summary.execution_backend!='firecracker' or summary.execution_metadata.get('agent_placement')!='guest':
            raise ValueError('Point is not complete-agent Firecracker execution: '+str(path))
        actual=summary.execution_metadata.get('memory_mib')
        if memory is not None and actual!=memory:raise ValueError(f'Memory mismatch: {path}: expected {memory}, actual {actual}')
        if len(summary.runs)!=summary.concurrency or summary.submitted_tasks!=summary.concurrency:
            raise ValueError('Task count differs from concurrency: '+str(path))
        for key in ['host_collection_status','host_gpu_collection_status','service_collection_status','guest_telemetry_collection_status','vmm_telemetry_collection_status','memory_bandwidth_collection_status']:
            if summary.telemetry_metrics.get(key)!='available':issues.append({'point':identity,'collector':key,'status':summary.telemetry_metrics.get(key)})
        intervals={'guest':[],'vmm':[]}
        for task in summary.runs:
            meta=task['execution_metadata'];metrics=task.get('telemetry_metrics',{});boot=meta.get('guest_boot_id')
            if boot:boot_ids.append(boot)
            if task['status']=='success':
                if not boot or meta.get('agent_placement')!='guest' or not meta.get('agent_pid') or meta.get('agent_pid')!=meta.get('model_client_pid'):
                    raise ValueError('Missing guest agent/model provenance: '+task['run_id'])
                validation=task.get('harness_validation_log',{})
                if validation.get('status')!='passed' or validation.get('guest_boot_id')!=boot:raise ValueError('Guest validation mismatch: '+task['run_id'])
                if task['model_calls']<1 or task['total_tokens']<1:raise ValueError('Successful task lacks model usage: '+task['run_id'])
                trajectory=Path(task['trajectory_path']);trajectory=trajectory if trajectory.is_absolute() else root/trajectory
                if not trajectory.is_file():raise ValueError('Missing trajectory: '+str(trajectory))
                for scope in ['guest','vmm']:
                    if metrics.get(scope+'_sample_count',0)<2:raise ValueError('Missing '+scope+' samples: '+task['run_id'])
                    sample=Path(metrics[scope+'_samples_path']);sample=sample if sample.is_absolute() else root/sample
                    samples=[json.loads(line) for line in sample.read_text().splitlines()]
                    intervals[scope].append((samples[0]['timestamp'],samples[-1]['timestamp']))
            proof.append({'run_id':task['run_id'],'workload':summary.workload,'concurrency':summary.concurrency,'status':task['status'],'guest_boot_id':boot,'model_calls':task['model_calls'],'total_tokens':task['total_tokens'],'error':task['error']})
        for scope,windows in intervals.items():
            active=peak=0
            for _,delta in sorted([(a,1) for a,b in windows]+[(b,-1) for a,b in windows]):active+=delta;peak=max(peak,active)
            summary.telemetry_metrics['observed_peak_'+scope+'_workers']=peak
        summaries.append(summary)
    if found!=expected:raise ValueError('Missing expected points: '+str(sorted(expected-found)))
    if len(boot_ids)!=len(set(boot_ids)):raise ValueError('Guest boot IDs reused across attempts')
    out.mkdir(parents=True,exist_ok=False);manifest=[]
    for summary in summaries:
        target=out/'points'/summary.workload/f'c{summary.concurrency:03d}';target.mkdir(parents=True)
        write_global_csv([summary],target);write_categorized_csvs([summary],target)
        manifest.append({'runset_id':run.name,'run_id':summary.workload+'_c'+str(summary.concurrency),'concurrency':summary.concurrency,'repeat':profile.get('repeat_index',1),'status':'completed','run_dir':str(target),'run_log':str(run/'benchmark.log'),'agent_cpuset':profile.get('vm_cpuset',profile.get('agent_vm_cpuset','')),'vllm_cpuset':profile.get('model_cpuset',''),'task_timeout':profile.get('task_timeout',profile.get('task_timeout_seconds','')),'exit_code':0})
    write_global_csv(summaries,out);write_categorized_csvs(summaries,out);write_csv(out/'manifest.csv',manifest)
    outputs=aggregate_standard_metrics(out)
    for path in [outputs.global_metrics,outputs.cpu_metrics,outputs.gpu_metrics,outputs.vllm_metrics]:
        if len(list(csv.DictReader(path.open())))!=len(expected):raise ValueError('Final table row count mismatch: '+str(path))
    for name in ['llm_calls.csv','latency_metrics.csv','guest_metrics.csv','vmm_metrics.csv','host_gpu_timeseries.csv']:
        rows=[]
        for workload,c in sorted(expected):
            path=run/workload/f'c{c:03d}'/name
            if path.is_file():rows.extend(csv.DictReader(path.open()))
            else:issues.append({'point':(workload,c),'missing_file':name})
        write_csv(out/name,rows)
    successes=sum(s.successful_tasks for s in summaries);attempts=sum(s.submitted_tasks for s in summaries)
    report={'complete_point_set':True,'all_tasks_successful':successes==attempts,'all_collectors_available':not issues,'core_collectors_available':not any(issue.get('collector') in {'host_collection_status','guest_telemetry_collection_status','vmm_telemetry_collection_status'} for issue in issues),'source_run':str(run),'memory_mib_per_vm':memory,'points':len(expected),'successful_tasks':successes,'task_attempts':attempts,'unique_guest_boot_ids':len(set(boot_ids)),'collector_issues':issues,'tasks':proof}
    (out/'validation.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='tasks'},indent=2));print('Summary:',out)
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,KeyError) as exc:print(str(exc),file=sys.stderr);raise SystemExit(2)
