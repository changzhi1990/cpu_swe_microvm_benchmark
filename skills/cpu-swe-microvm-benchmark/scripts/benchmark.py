#!/usr/bin/env python3
"""Portable entrypoint: discover -> prepare a clean workspace -> smoke/run -> validate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen

SKILL=Path(__file__).resolve().parents[1]
MANIFEST=json.loads((SKILL/'assets/runtime-manifest.json').read_text())


def command(args, **kwargs):
    print('+',' '.join(map(str,args)),flush=True)
    return subprocess.run(list(map(str,args)),check=True,**kwargs)


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def download(url,path,expected=None):
    path=Path(path);part=path.with_suffix(path.suffix+'.partial')
    with urlopen(url,timeout=120) as response,part.open('wb') as output:shutil.copyfileobj(response,output)
    if expected and digest(part)!=expected:
        part.unlink();raise ValueError('Downloaded artifact checksum mismatch')
    part.replace(path)


def doctor(workspace):
    report={'platform':platform.system(),'architecture':platform.machine(),'python':platform.python_version(),'workspace':str(workspace),'kvm_access':os.access('/dev/kvm',os.R_OK|os.W_OK),'tools':{name:shutil.which(name) for name in ['docker','firecracker','mkfs.ext4','sudo','nvidia-smi','dcgmi','AMDuProfPcm']}}
    if hasattr(os,'sched_getaffinity'):report['allowed_cpu_ids']=sorted(os.sched_getaffinity(0))
    if Path('/proc/meminfo').exists():
        report['memory_mib']={line.split(':')[0]:int(line.split()[1])/1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}
    parent=workspace
    while not parent.exists():parent=parent.parent
    report['disk_free_gib']=shutil.disk_usage(parent).free/1024**3
    if shutil.which('lscpu'):report['cpu_topology']=subprocess.check_output(['lscpu','-p=CPU,CORE,SOCKET,NODE'],text=True)
    if shutil.which('nvidia-smi'):
        result=subprocess.run(['nvidia-smi','--query-gpu=index,uuid,name,memory.total,driver_version','--format=csv'],capture_output=True,text=True)
        report['gpu_inventory']=result.stdout.strip() if result.returncode==0 else {'unavailable':result.stderr.strip()}
    report['execution_supported']=report['platform']=='Linux' and report['architecture']=='x86_64' and report['kvm_access']
    return report


def prepare(args):
    workspace=args.workspace.resolve();state=doctor(workspace)
    if not state['execution_supported']:raise ValueError('Preparation requires Linux x86_64 and read/write /dev/kvm; use a supported remote host')
    missing=[name for name in ['docker','mkfs.ext4','sudo'] if not state['tools'][name]]
    if missing:raise ValueError('Install missing host prerequisites: '+', '.join(missing))
    if (workspace/'prepared.json').exists():raise ValueError('Workspace already prepared; use its runtime or choose a new workspace')
    workspace.mkdir(parents=True,exist_ok=True)
    runtime=workspace/'runtime'
    if runtime.exists():raise ValueError('Runtime directory already exists; refusing to overwrite it')
    shutil.copytree(SKILL/'assets/runtime',runtime,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    (workspace/'environment.json').write_text(json.dumps(state,indent=2))
    tools=workspace/'tools';tools.mkdir(exist_ok=True)
    binary=shutil.which('firecracker')
    if not binary:
        archive=tools/'firecracker.tgz';version=MANIFEST['tested_firecracker']
        download(f'https://github.com/firecracker-microvm/firecracker/releases/download/v{version}/firecracker-v{version}-x86_64.tgz',archive)
        with tarfile.open(archive) as source:
            entries=[entry for entry in source if entry.isfile() and Path(entry.name).name=='firecracker-v'+version+'-x86_64']
            if len(entries)!=1:raise ValueError('Release archive did not contain the expected Firecracker executable')
            binary=str(tools/'firecracker');Path(binary).write_bytes(source.extractfile(entries[0]).read());Path(binary).chmod(0o755)
    assets=workspace/'images';assets.mkdir(exist_ok=True)
    kernel=assets/'vmlinux'
    if args.kernel_file:
        shutil.copy2(args.kernel_file,kernel)
    else:download(MANIFEST['kernel_url'],kernel,MANIFEST['kernel_sha256'])
    venv=workspace/'.venv'
    command([sys.executable,'-m','venv',venv])
    python=venv/'bin/python'
    command([python,'-m','pip','install',str(runtime)])
    image='microvm-benchmark-guest:'+hashlib.sha256((runtime/'scripts/microvm/Dockerfile').read_bytes()).hexdigest()[:12]
    command(['docker','build','-f',runtime/'scripts/microvm/Dockerfile','-t',image,str(runtime)])
    env=dict(os.environ);env['MINI_SWE_AGENT_SRC']=str(runtime/'vendor/mini-swe-agent/src')
    command(['bash',runtime/'scripts/microvm/build_guest.sh',image,assets/'guest'],env=env)
    info={'runtime':str(runtime),'python':str(python),'kernel':str(kernel),'rootfs':str(assets/'guest/rootfs.ext4'),'firecracker_binary':str(Path(binary).resolve()),'kernel_sha256':digest(kernel),'rootfs_sha256':digest(assets/'guest/rootfs.ext4'),'firecracker_sha256':digest(binary),'agent_source':'bundled mini-swe-agent snapshot; see assets/runtime-manifest.json','guest_image_id':subprocess.check_output(['docker','image','inspect',image,'--format','{{.Id}}'],text=True).strip()}
    info['firecracker_version']=subprocess.check_output([binary,'--version'],text=True).strip()
    (workspace/'host-requirements.txt').write_text(subprocess.check_output([python,'-m','pip','freeze'],text=True))
    (workspace/'prepared.json').write_text(json.dumps(info,indent=2))
    print(json.dumps(info,indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['doctor','prepare','smoke','run','summarize'])
    p.add_argument('--workspace',required=True,type=Path)
    p.add_argument('--kernel-file',type=Path,help='Optional compatible guest kernel; otherwise download the pinned artifact')
    p.add_argument('--base-url',default=os.getenv('BASE_URL'))
    p.add_argument('--model',default=os.getenv('MODEL'))
    p.add_argument('--concurrency-levels',default='1,2,4,8,32,64,128')
    p.add_argument('--repeats',type=int,default=1)
    p.add_argument('--run-dir',type=Path)
    args,extra=p.parse_known_args()
    workspace=args.workspace.resolve()
    if args.action=='doctor':
        print(json.dumps(doctor(workspace),indent=2));return 0
    if args.action=='prepare':prepare(args);return 0
    info=json.loads((workspace/'prepared.json').read_text())
    if args.action=='summarize':
        if args.run_dir is None:raise ValueError('summarize requires --run-dir')
        return command([info['python'],SKILL/'scripts/summarize_run.py',args.run_dir,'--bench-root',info['runtime'],*extra]).returncode
    if not args.base_url or not args.model:raise ValueError('Provide --base-url and --model (or BASE_URL and MODEL environment variables)')
    if args.repeats<1:raise ValueError('repeats must be positive')
    if args.action=='smoke' and args.repeats!=1:raise ValueError('Smoke is one c1 trial; use run for repeats')
    count=1 if args.action=='smoke' else args.repeats
    levels='1' if args.action=='smoke' else args.concurrency_levels
    if count>1 and '--output-dir' in extra:raise ValueError('For repeats omit --output-dir; each trial gets a fresh directory')
    for repeat in range(count):
        print(f'Trial {repeat+1}/{count}',flush=True)
        env=dict(os.environ);env['FIRECRACKER_BINARY']=info['firecracker_binary'];env['BENCHMARK_REPEAT_INDEX']=str(repeat+1);env['BENCHMARK_REPEAT_COUNT']=str(count)
        command([info['python'],SKILL/'scripts/run_sweep.py','--bench-root',info['runtime'],'--kernel',info['kernel'],'--rootfs',info['rootfs'],'--base-url',args.base_url,'--model',args.model,'--concurrency-levels',levels,*extra],env=env)
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,subprocess.CalledProcessError) as exc:print(str(exc),file=sys.stderr);raise SystemExit(2)
