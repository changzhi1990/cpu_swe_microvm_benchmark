from cpu_swe_benchmark.microvm_environment import MicroVMConfig


def add_execution_options(parser):
    parser.add_argument('--execution-backend', choices=['native', 'firecracker'], default='native')
    parser.add_argument('--microvm-kernel', default='')
    parser.add_argument('--microvm-rootfs', default='')
    parser.add_argument('--firecracker-binary', default='firecracker')
    parser.add_argument('--vcpu-per-vm', type=int, default=2)
    parser.add_argument('--memory-mib-per-vm', type=int, default=2048)
    parser.add_argument('--vm-boot-timeout', type=int, default=60)
    parser.add_argument('--vm-cpuset', default='', help='Host CPU affinity shared by all VM processes; not per-VM exclusive pinning')
    parser.add_argument('--max-parallel-vm-starts', type=int, default=4)


def execution_options(args):
    config = None
    if args.execution_backend == 'firecracker':
        config = MicroVMConfig(kernel=args.microvm_kernel, rootfs=args.microvm_rootfs, binary=args.firecracker_binary, vcpus=args.vcpu_per_vm, memory_mib=args.memory_mib_per_vm, boot_timeout=args.vm_boot_timeout, cpuset=args.vm_cpuset)
        config.validate()
    return {'execution_backend': args.execution_backend, 'microvm_config': config, 'max_parallel_vm_starts': args.max_parallel_vm_starts}
