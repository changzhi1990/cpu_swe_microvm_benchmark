"""Firecracker cold-boot execution backend for trusted benchmark workloads.

The agent/model client run in the guest; the host owns VM orchestration. A task owns one writable disk and VM.
This research launcher is unprivileged, offline, and NOT a production jailer.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import secrets
import socket
import subprocess
import tempfile
import time
from typing import Any
import tarfile
from cpu_swe_benchmark.model_bridge import host_model_bridge
from cpu_swe_benchmark.telemetry import TimelineSampler, ProcessReader

MAX_BYTES = 64 * 1024 * 1024

@dataclass(frozen=True)
class MicroVMConfig:
    kernel: str
    rootfs: str
    binary: str = 'firecracker'
    vcpus: int = 2
    memory_mib: int = 2048
    boot_timeout: int = 60
    cpuset: str = ''

    def validate(self):
        if self.vcpus < 1 or self.memory_mib < 128 or self.boot_timeout < 1:
            raise ValueError('VM vCPUs/boot timeout must be positive; memory must be >=128 MiB')
        for path in (self.kernel, self.rootfs):
            if not Path(path).is_file():
                raise FileNotFoundError(path)
        if not shutil.which(self.binary):
            raise FileNotFoundError(self.binary)
        if not os.access('/dev/kvm', os.R_OK | os.W_OK):
            raise PermissionError('Read/write access to /dev/kvm is required; no native fallback')


def image_identity(config: MicroVMConfig) -> dict[str, Any]:
    config.validate()
    identity: dict[str, Any] = asdict(config)
    for label, name in (('kernel', config.kernel), ('rootfs', config.rootfs)):
        digest = hashlib.sha256()
        with open(name, 'rb') as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b''):
                digest.update(chunk)
        identity[label + '_sha256'] = digest.hexdigest()
    version = subprocess.run([config.binary, '--version'], capture_output=True, text=True, check=True)
    identity['firecracker_version'] = version.stdout.strip()
    identity['agent_placement'] = 'guest'
    identity['sandbox_mode'] = 'cold_boot'
    identity['network'] = 'model_endpoint_vsock_only'
    identity['host_cpuset'] = sorted(os.sched_getaffinity(0))
    return identity


def extract_workspace(encoded: str, destination: Path):
    """Never restore guest-controlled links or paths outside the output directory."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded)), mode='r:gz') as archive:
        for item in archive:
            target = (root / item.name).resolve()
            if not target.is_relative_to(root) or not item.isfile():
                raise ValueError('Unsafe guest artifact member')
            total += item.size
            if total > MAX_BYTES:
                raise ValueError('Guest artifacts exceed size limit')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(item).read())
            target.chmod(item.mode & 0o777)


@dataclass
class GuestCommandConfig:
    cwd: str
    env: dict[str, str]
    timeout: int

    def model_dump(self, **kwargs):
        return asdict(self)


class MicroVMEnvironment:
    def __init__(self, vm: MicroVMConfig, *, run_dir: Path, timeout: int, env: dict[str, str]):
        vm.validate()
        self.vm = vm
        # Host Python's PATH must not leak into the guest.
        guest_env = {key: value for key, value in env.items() if key != 'PATH'}
        self.config = GuestCommandConfig(cwd='/workspace', env=guest_env, timeout=timeout)
        self.run_dir = run_dir.resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.process = None
        self.temp_dir = None
        self.log = None
        self.model_bridge = None
        self.telemetry = None
        self.closed = False
        self.metadata: dict[str, Any] = {'backend': 'firecracker', 'agent_placement': 'guest', 'sandbox_mode': 'cold_boot', 'vcpu_per_vm': vm.vcpus, 'memory_mib_per_vm': vm.memory_mib, 'vm_cpuset': vm.cpuset, 'numa_policy': 'host_default', 'jailer_enabled': False}

    def start(self, workspace: Path, *, model_endpoint: str | None = None):
        started = time.monotonic()
        try:
            self.temp_dir = Path(tempfile.mkdtemp(prefix='csw-'))
            disk = self.temp_dir / 'rootfs.ext4'
            subprocess.run(['cp', '--reflink=auto', '--sparse=always', self.vm.rootfs, str(disk)], check=True)
            self.socket_path = str(self.temp_dir / 'vsock.sock')
            if model_endpoint is not None:
                self.model_bridge, self.guest_model_endpoint = host_model_bridge(self.socket_path + '_9000', model_endpoint)
            machine = {
                'boot-source': {'kernel_image_path': str(Path(self.vm.kernel).resolve()), 'boot_args': 'console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/sbin/cpu-swe-init'},
                'drives': [{'drive_id': 'rootfs', 'path_on_host': str(disk), 'is_root_device': True, 'is_read_only': False}],
                'machine-config': {'vcpu_count': self.vm.vcpus, 'mem_size_mib': self.vm.memory_mib},
                'vsock': {'guest_cid': secrets.randbelow(2**31 - 4) + 3, 'uds_path': self.socket_path},
            }
            machine_path = self.temp_dir / 'machine.json'
            machine_path.write_text(json.dumps(machine))
            command = [self.vm.binary, '--no-api', '--config-file', str(machine_path)]
            if self.vm.cpuset:
                command = ['taskset', '-c', self.vm.cpuset, *command]
            self.log = (self.run_dir / 'firecracker.log').open('w')
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT)
            self.metadata['vmm_pid'] = self.process.pid
            self.telemetry = TimelineSampler(self.run_dir / 'vmm_metrics.jsonl', ProcessReader(self.process.pid), scope='vmm', interval=0.5)
            self.telemetry.start()
            deadline = time.monotonic() + self.vm.boot_timeout
            while True:
                if self.process.poll() is not None:
                    raise RuntimeError('Firecracker exited during boot; see firecracker.log')
                try:
                    info = self.rpc({'op': 'ping'}, timeout=1)
                    self.metadata['guest_kernel'] = info['kernel']
                    break
                except (OSError, RuntimeError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError('microVM readiness deadline exceeded')
                    time.sleep(0.1)
            self.metadata['sandbox_ready_seconds'] = time.monotonic() - started
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w:gz') as archive:
                for path in workspace.rglob('*'):
                    if path.is_file() and not path.is_symlink():
                        archive.add(path, arcname=str(path.relative_to(workspace)), recursive=False)
            self.rpc({'op': 'seed', 'archive': base64.b64encode(stream.getvalue()).decode()}, timeout=30)
            self.metadata['sandbox_prepare_seconds'] = time.monotonic() - started
        except BaseException:
            self.close()
            raise

    def rpc(self, request: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(self.socket_path)
            connection.sendall(b'CONNECT 8000\n')
            stream = connection.makefile('rb')
            if not stream.readline(128).startswith(b'OK '):
                raise RuntimeError('vsock handshake failed')
            connection.sendall(json.dumps(request).encode() + b'\n')
            line = stream.readline(MAX_BYTES * 2 + 1)
            if len(line) > MAX_BYTES * 2:
                raise RuntimeError('Guest response exceeds size limit')
            if not line:
                raise RuntimeError('Guest disconnected')
            result = json.loads(line)
            if 'rpc_error' in result:
                raise RuntimeError(result['rpc_error'])
            return result

    def execute_raw(self, command: str, *, cwd: str = '', timeout: int | None = None):
        limit = timeout or self.config.timeout
        return self.rpc({'op': 'exec', 'command': command, 'cwd': cwd or self.config.cwd, 'timeout': limit, 'env': self.config.env}, timeout=limit + 10)

    def execute(self, action: dict, cwd: str = '', *, timeout: int | None = None):
        from minisweagent.environments.local import LocalEnvironment
        output = self.execute_raw(action.get('command', ''), cwd=cwd, timeout=timeout)
        LocalEnvironment._check_finished(self, output)
        return output

    def export_workspace(self, destination: Path):
        started = time.monotonic()
        extract_workspace(self.rpc({'op': 'export'}, timeout=60)['archive'], destination)
        self.metadata['artifact_export_seconds'] = time.monotonic() - started

    def close(self):
        if self.closed:
            return
        self.closed = True
        started = time.monotonic()
        if self.telemetry is not None:
            self.metadata.update(self.telemetry.stop())
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.model_bridge is not None:
            self.model_bridge.close()
        if self.log:
            self.log.close()
        if self.temp_dir:
            shutil.rmtree(self.temp_dir)
            self.temp_dir = None
        self.metadata['sandbox_destroy_seconds'] = time.monotonic() - started

    def get_template_vars(self, **kwargs):
        return {**self.config.model_dump(), 'system': 'Linux', **kwargs}

    def serialize(self):
        return {'info': {'config': {'environment': self.config.model_dump(), 'environment_type': 'firecracker'}, 'microvm': self.metadata}}
