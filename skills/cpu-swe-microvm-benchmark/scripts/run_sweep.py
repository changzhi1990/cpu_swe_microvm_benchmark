#!/usr/bin/env python3
"""Plan/run the implemented complete-agent Firecracker backend without recording credentials."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time
import traceback
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def levels(value):
    result=[int(part.strip()) for part in value.split(',') if part.strip()]
    if not result or min(result)<1 or len(set(result))!=len(result):
        raise argparse.ArgumentTypeError('Concurrency points must be unique positive integers')
    return result


def cpus(value):
    result=set()
    for part in value.split(','):
        if not part:continue
        bounds=part.split('-')
        start=int(bounds[0]);end=int(bounds[-1])
        if start<0 or end<start or len(bounds)>2:raise ValueError('Invalid CPU set')
        result.update(range(start,end+1))
    return result


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bench-root',required=True,type=Path)
    p.add_argument('--kernel',required=True,type=Path)
    p.add_argument('--rootfs',required=True,type=Path)
    p.add_argument('--workload',default='algorithm_lab_sorting_bugfix',choices=['algorithm_lab_sorting_bugfix','memory_lab_bandwidth_bugfix','all'])
    p.add_argument('--concurrency-levels',type=levels,default=levels('1,2,4,8,32,64,128'))
    p.add_argument('--memory-mib',type=int,default=2048)
    p.add_argument('--vcpus',type=int,default=2)
    p.add_argument('--vm-cpuset',default='')
    p.add_argument('--model-cpuset',default='')
    p.add_argument('--base-url',default=os.getenv('BASE_URL'))
    p.add_argument('--model',default=os.getenv('MODEL'))
    p.add_argument('--max-parallel-vm-starts',type=int,default=4)
    p.add_argument('--task-timeout',type=int,default=600)
    p.add_argument('--command-timeout',type=int,default=120)
    p.add_argument('--max-tokens',type=int,default=1024)
    p.add_argument('--cpu-threads-per-worker',type=int,default=1)
    p.add_argument('--temperature',type=float,default=0.0)
    p.add_argument('--headroom-mib',type=int,default=16384)
    p.add_argument('--allow-memory-overcommit',action='store_true')
    p.add_argument('--output-dir',type=Path)
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--require-service-metrics',action='store_true')
    return p


def plan(args):
    root=args.bench_root.resolve()
    for name in ['benchmark_latency.py','src/cpu_swe_benchmark/guest_worker.py','src/cpu_swe_benchmark/telemetry.py']:
        if not (root/name).is_file():raise ValueError('Implemented benchmark backend missing: '+str(root/name))
    for path in [args.kernel,args.rootfs]:
        if not path.is_file():raise ValueError('Missing image/kernel: '+str(path))
    if min(args.vcpus,args.memory_mib,args.task_timeout,args.command_timeout,args.max_parallel_vm_starts,args.max_tokens,args.cpu_threads_per_worker)<1 or args.headroom_mib<0:
        raise ValueError('Resource and timeout arguments must be positive; headroom must be nonnegative')
    if not args.base_url or not args.model:raise ValueError('Provide the customer model endpoint and served model name')
    parsed=urlsplit(args.base_url)
    if parsed.scheme!='http' or not parsed.hostname or parsed.username or parsed.password:raise ValueError('Use an HTTP endpoint without embedded credentials')
    allowed=set(os.sched_getaffinity(0));selected=cpus(args.vm_cpuset) if args.vm_cpuset else allowed
    if not selected or not selected.issubset(allowed):raise ValueError('VM cpuset is outside this process CPU affinity')
    model=cpus(args.model_cpuset)
    if selected&model:raise ValueError('VM/model CPU sets overlap')
    siblings=set()
    for cpu in model:
        path=Path(f'/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list')
        if path.is_file():siblings.update(cpus(path.read_text().strip()))
    if selected&siblings:raise ValueError('VM cpuset overlaps SMT siblings of reserved model CPUs')
    available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))//1024
    committed=max(args.concurrency_levels)*args.memory_mib
    issues=[]
    if committed+args.headroom_mib>available and not args.allow_memory_overcommit:
        issues.append(f'Memory policy: configured {committed} MiB + {args.headroom_mib} MiB headroom > {available} MiB available; choose an explicit memory-overcommit policy or a fitting experiment')
    if not os.access('/dev/kvm',os.R_OK|os.W_OK):issues.append('Read/write access to /dev/kvm is unavailable')
    if not shutil.which(os.getenv('FIRECRACKER_BINARY','firecracker')):issues.append('Firecracker executable unavailable')
    output=(args.output_dir or root/'results'/('microvm_'+args.workload+'_'+time.strftime('%Y%m%d_%H%M%S'))).resolve()
    if output.exists():issues.append('Output already exists; choose a new directory')
    return {**{key:(str(value.resolve()) if isinstance(value,Path) else value) for key,value in vars(args).items() if key not in {'dry_run','output_dir'}},'bench_root':str(root),'output_dir':str(output),'agent_placement':'guest','execution_backend':'firecracker','sandbox_mode':'cold_boot','vm_effective_cpu_count':len(selected),'configured_peak_memory_mib':committed,'available_memory_mib':available,'cpu_vcpu_oversubscription':max(args.concurrency_levels)*args.vcpus/len(selected),'disk_rootfs_allocated_mib':args.rootfs.stat().st_blocks*512/1024**2,'temporary_disk_available_mib':shutil.disk_usage(os.getenv('TMPDIR','/tmp')).free/1024**2,'services_checked':False,'ready_to_run':not issues,'issues':issues}


def main(argv=None):
    args=parser().parse_args(argv)
    profile=plan(args)
    profile['repeat_index']=int(os.getenv('BENCHMARK_REPEAT_INDEX','1'));profile['repeat_count']=int(os.getenv('BENCHMARK_REPEAT_COUNT','1'))
    if args.dry_run:
        print(json.dumps(profile,indent=2));return 0
    if not profile['ready_to_run']:raise ValueError('; '.join(profile['issues']))
    key=os.getenv('API_KEY','token-abc123')
    origin=urlsplit(args.base_url)
    for url in [args.base_url.rstrip('/')+'/models']:
        with urlopen(Request(url,headers={'Authorization':'Bearer '+key}),timeout=10) as response:
            if response.status!=200:raise ValueError('Service not ready: '+url)
            body=response.read().decode()
            if url.endswith('/models') and args.model not in {row['id'] for row in json.loads(body)['data']}:
                raise ValueError('Requested model not served')
            if url.endswith('/metrics') and 'vllm:' not in body:raise ValueError('Expected vLLM service metrics missing')
    try:
        with urlopen(Request(f'{origin.scheme}://{origin.netloc}/metrics'),timeout=5) as response:
            if 'vllm:' not in response.read().decode():raise ValueError('No vLLM service counters')
        profile['service_metrics_available']=True
    except Exception as exc:
        profile['service_metrics_available']=False
        profile['service_metrics_error']=str(exc)
        if args.require_service_metrics:raise ValueError('Required service metrics unavailable')
    profile['services_checked']=True
    root=Path(profile['bench_root']);output=Path(profile['output_dir']);output.mkdir(parents=True,exist_ok=False)
    digest=hashlib.sha256()
    for path in sorted((root/'src/cpu_swe_benchmark').glob('*.py')):
        digest.update(path.name.encode());digest.update(path.read_bytes())
    profile['backend_source_sha256']=digest.hexdigest();profile['api_key_source']='environment' if 'API_KEY' in os.environ else 'local_test_default'
    (output/'run_profile.json').write_text(json.dumps(profile,indent=2));(output/'started_at.txt').write_text(time.strftime('%Y-%m-%dT%H:%M:%S%z'))
    soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE);resource.setrlimit(resource.RLIMIT_NOFILE,(max(soft,min(65536,hard)) if hard!=resource.RLIM_INFINITY else max(soft,65536),hard))
    if args.vm_cpuset:os.sched_setaffinity(0,cpus(args.vm_cpuset))
    os.chdir(root);sys.path.insert(0,str(root));sys.path.insert(0,str(root/'src'))
    from benchmark_latency import LatencyBenchmarker
    from cpu_swe_benchmark.microvm_environment import MicroVMConfig
    from cpu_swe_benchmark.workloads import parse_workload_list
    code=0
    with (output/'benchmark.log').open('w',buffering=1) as log,contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
        try:
            bench=LatencyBenchmarker({'model_name':args.model,'base_url':args.base_url,'api_key':key,'max_tokens':args.max_tokens,'temperature':args.temperature},output,execution_backend='firecracker',microvm_config=MicroVMConfig(profile['kernel'],profile['rootfs'],vcpus=args.vcpus,memory_mib=args.memory_mib,cpuset=args.vm_cpuset,binary=os.getenv('FIRECRACKER_BINARY','firecracker')),max_parallel_vm_starts=args.max_parallel_vm_starts,task_timeout_seconds=args.task_timeout,command_timeout_seconds=args.command_timeout,cpu_threads_per_worker=args.cpu_threads_per_worker)
            bench.run_workloads([workload.name for workload in parse_workload_list(args.workload)],args.concurrency_levels)
        except BaseException:
            traceback.print_exc();code=1
    (output/'exit_code.txt').write_text(str(code));(output/'ended_at.txt').write_text(time.strftime('%Y-%m-%dT%H:%M:%S%z'))
    print('Output:',output,'benchmark exit:',code)
    if code:return code
    return subprocess.run([sys.executable,str(Path(__file__).with_name('summarize_run.py')),str(output),'--bench-root',str(root),'--expected-memory-mib',str(args.memory_mib)],check=False).returncode


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError) as exc:print(str(exc),file=sys.stderr);raise SystemExit(2)
