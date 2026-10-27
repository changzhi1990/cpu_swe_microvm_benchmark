#!/usr/bin/env python3
"""Trusted benchmark guest RPC service. No host filesystem is mounted."""
import base64
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tarfile

WORKSPACE = Path('/workspace')
MAX_BYTES = 64 * 1024 * 1024
TASK_STARTED = False

def unpack(data):
    WORKSPACE.mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(data)), mode='r:gz') as archive:
        total = 0
        for item in archive:
            target = (WORKSPACE / item.name).resolve()
            if not target.is_relative_to(WORKSPACE) or not (item.isfile() or item.isdir()):
                raise ValueError('Unsafe workspace archive member')
            total += item.size
            if total > MAX_BYTES:
                raise ValueError('Workspace exceeds size limit')
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(item).read())
                target.chmod(item.mode & 0o777)
    return {'ready': True}

def dispatch(request):
    global TASK_STARTED
    op = request['op']
    if op == 'ping':
        return {'ready': True, 'pid': os.getpid(), 'kernel': os.uname().release, 'capabilities': ['full_agent']}
    if op == 'seed':
        return unpack(request['archive'])
    if op == 'export':
        stream = io.BytesIO()
        total = 0
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for path in WORKSPACE.rglob('*'):
                if path.is_symlink() or not path.is_file():
                    continue
                if '.git' in path.parts or '__pycache__' in path.parts or '.pytest_cache' in path.parts:
                    continue
                total += path.stat().st_size
                if total > MAX_BYTES:
                    raise ValueError('Export exceeds size limit')
                archive.add(path, arcname=str(path.relative_to(WORKSPACE)), recursive=False)
        return {'archive': base64.b64encode(stream.getvalue()).decode()}
    if op == 'run_task':
        if TASK_STARTED:
            raise RuntimeError('Only one complete agent attempt is permitted per VM')
        TASK_STARTED = True
        output_dir = WORKSPACE / 'attempt'
        output_dir.mkdir(exist_ok=True)
        log_path = output_dir / 'agent.log'
        with log_path.open('wb') as log:
            process = subprocess.Popen(['/usr/bin/python3', '/opt/cpu-swe/guest_task.py'], stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                process.communicate(json.dumps(request['config']).encode(), timeout=request['timeout'])
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                return {'task_error': 'Guest agent hard deadline exceeded', 'timed_out': True}
        path = output_dir / 'run_result.json'
        if process.returncode != 0 or not path.is_file():
            return {'task_error': f'Guest agent exited {process.returncode}; see guest agent.log'}
        return {'run_result': json.loads(path.read_text())}
    if op != 'exec':
        raise ValueError('Unknown RPC operation')
    env = dict(os.environ)
    env.update(request.get('env', {}))
    process = subprocess.Popen(['/bin/bash', '-c', request['command']], cwd=request.get('cwd') or str(WORKSPACE), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=request['timeout'])
        return {'output': output.decode(errors='replace'), 'returncode': process.returncode, 'exception_info': ''}
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        return {'output': output.decode(errors='replace'), 'returncode': -1, 'exception_info': 'Command timed out; process group killed', 'timed_out': True}

def main():
    from cpu_swe_benchmark.model_bridge import guest_model_bridge
    model_bridge = guest_model_bridge()
    listener = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    listener.bind((socket.VMADDR_CID_ANY, 8000))
    listener.listen(16)
    while True:
        connection, _ = listener.accept()
        with connection:
            try:
                stream = connection.makefile('rb')
                line = stream.readline(MAX_BYTES * 2 + 1)
                if len(line) > MAX_BYTES * 2:
                    raise ValueError('RPC request too large')
                result = dispatch(json.loads(line))
                connection.sendall(json.dumps(result).encode() + b'\n')
            except Exception as exc:
                connection.sendall(json.dumps({'rpc_error': f'{type(exc).__name__}: {exc}'}).encode() + b'\n')

if __name__ == '__main__':
    main()
