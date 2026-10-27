# CPU SWE MicroVM Benchmark

基于 Firecracker 的 coding-agent 基准测试工程，以及客户可直接使用的自包含 skill。
每个 microVM 内运行一个完整的 mini-swe-agent：在 guest 内调用模型、查看和修改代码、执行测试，并由 guest harness 验收。宿主机负责 VM 编排、vsock 模型请求转发、硬件采样及结果导出。

默认每 VM **2 vCPU / 2048 MiB RAM**，默认任务为 `algorithm_lab_sorting_bugfix`，并发点为 `1,2,4,8,32,64,128`。可配置参数、实验控制及指标口径见 [SKILL.md](skills/cpu-swe-microvm-benchmark/SKILL.md)。

## 环境要求

- Linux x86_64，支持 KVM，当前用户可读写 `/dev/kvm`。
- Python 3.10+、venv、可用的 Docker daemon、`mkfs.ext4`、mount/chroot 及 sudo。
- 可访问的 OpenAI-compatible HTTP 模型 endpoint，以及实际 served model name。
- 足够的 RAM 和磁盘；默认检查最大并发所需 guest RAM，并保留 16 GiB 宿主机余量。128 个 2 GiB guest 合计配置 256 GiB RAM。

验证过的组合为 Firecracker 1.16.0、Ubuntu 26.10 guest、Linux 6.1.188、mini-swe-agent 2.4.5。准备步骤下载依赖并构建 guest，不包含模型部署。详细说明见 [部署与执行](skills/cpu-swe-microvm-benchmark/references/execution.md)。

## 快速开始

在 Linux 测试主机执行：

```bash
git clone https://github.com/changzhi1990/cpu_swe_microvm_benchmark.git
cd cpu_swe_microvm_benchmark
export WORKDIR="$HOME/microvm-benchmark-work"
export BASE_URL="http://MODEL_HOST:8000/v1"
export MODEL="YOUR_SERVED_MODEL_NAME"
# 服务需要鉴权时，在当前 shell 设置临时 API_KEY 环境变量。
python3 benchmark.py doctor --workspace "$WORKDIR"
python3 benchmark.py prepare --workspace "$WORKDIR"
python3 benchmark.py smoke --workspace "$WORKDIR" --base-url "$BASE_URL" --model "$MODEL"
python3 benchmark.py run --workspace "$WORKDIR" \
  --base-url "$BASE_URL" --model "$MODEL" \
  --concurrency-levels 1,2,4,8,32,64,128 --repeats 3 --dry-run
```

审阅 dry-run 的资源和实验配置后，删除 `--dry-run` 执行。先通过 c1 smoke，再执行正式 sweep。已有兼容 kernel 可在 prepare 时传入 `--kernel-file /absolute/path/to/vmlinux`。工作目录应位于源码目录之外；镜像、虚拟环境和运行结果均生成在该工作目录。

## 使用 skill

将 `skills/cpu-swe-microvm-benchmark` 整个文件夹复制到 agent 的 skills 目录，例如 Codex 的 `~/.codex/skills/`，然后调用 `$cpu-swe-microvm-benchmark` 并提供测试主机、工作目录、模型 endpoint 和名称。

该目录包括方法论、实验设计、指标定义、执行脚本、runtime、任务模板、mini-swe-agent 源码和第三方许可证；可独立分发，无需作者测试机或其他 checkout。

## 指标和结果

- LLM：调用次数、输入/输出 token、请求耗时、客户端 TTFT/TPOT、重试及错误。
- Latency：完整任务 E2E、VM 准备和启动、agent、工具、验收、导出和清理阶段。
- 资源：guest CPU/RAM、agent RSS、宿主机 VMM 进程 CPU/RSS、host CPU/RAM 和可用的内存带宽。
- GPU/服务：NVIDIA GPU 利用率、显存、功耗等；可访问时采集 vLLM Prometheus。

每次运行独立保存原始日志、trajectory、代码和测试证据；`validated/` 保存校验和汇总。缺失的增强采集器应记录原因。远程模型的 GPU 必须在模型节点另行采集；共享服务的 GPU 指标不应均分为每 VM 指标。分析以成功任务吞吐和成功率为主，同时解释延迟与资源开销。CPU 利用率随并发上升并不保证成功吞吐持续提升。

当前 runner 实现 cold-boot 单波测试，没有 snapshot、ready-pool 或 steady-state 模式；未配置 jailer，仅用于可信 benchmark 工作负载。HTTPS-only 模型服务和非 NVIDIA GPU 采集需要额外 transport/collector 适配。

## 源码与开发

```text
benchmark.py                     客户入口
benchmark_latency.py             底层 benchmark 入口
src/cpu_swe_benchmark/            编排、执行和遥测
scripts/microvm/                  guest 镜像构建与 guest 服务
repo_templates/                  确定性 bugfix 工作负载
vendor/mini-swe-agent/            agent 源码快照与 MIT 许可证
skills/cpu-swe-microvm-benchmark/  可独立分发的 skill
tests/                          runtime 测试
tools/sync_skill_runtime.py      同步 skill 内的 runtime
```

根目录 runtime 是源码维护入口。修改后同步，并检查自包含副本：

```bash
python3 -m pip install -e '.[test]'
python3 tools/sync_skill_runtime.py
python3 tools/sync_skill_runtime.py --check
python3 -m pytest tests -q
```

CI 验证单元测试和 runtime 同步；实际 KVM、guest、模型和 GPU 链路需在测试主机运行 smoke 验证。仓库不包含模型权重、VM 镜像、客户凭据或历史测试结果。第三方来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
