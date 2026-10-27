---
name: cpu-swe-microvm-benchmark
description: Design, prepare, execute, and analyze reproducible coding-agent benchmarks using one complete mini-swe-agent per Firecracker microVM. Use when a customer wants to measure useful task throughput, latency, CPU, memory, GPU, or model-serving behavior across concurrency points or machines. Includes a portable runtime and workload templates; no author-specific server or unpublished checkout is required.
---

# MicroVM Agent Benchmark Methodology

帮助客户在自己的环境中完成可复现的 agentic coding 测试：发现环境 → 固化实验方案 → 准备完整 guest → smoke test → 执行 sweep → 校验正确性和采集质量 → 分析并交付。

## 测试对象与边界

每个 microVM 运行一个完整 mini-swe-agent，独立完成模型调用、决策、查看/修改代码、执行测试、提交和最终验收。宿主机只负责 VM 编排、vsock 字节转发、硬件采样、制品导出与汇总。一个新 attempt 对应一个新 VM、独立工作目录和可写磁盘；同 attempt 的命令保留状态。

客户可以使用本机或远程模型服务。GPU 通常由共享模型服务使用；不能把共享 GPU 利用率平均分配成“每 VM GPU 利用率”。模型在远程时，测试宿主机的 GPU 数据不代表模型服务器的 GPU 数据。

随包提供 runtime、mini-swe-agent 源码快照及 MIT 许可证、确定性工作负载和执行入口。无需客户获取作者测试机、私有路径或未发布分支。需要支持的平台与工具仍须满足 [部署条件](references/execution.md)。

## 1. 发现环境和需求

先从当前主机、已有连接及客户提供的配置发现：Linux/KVM、CPU/SMT/NUMA、可用 RAM、临时磁盘、GPU/驱动、Docker、模型 endpoint 和 served model name。仅询问无法发现且确实影响执行的输入；不要求客户修改 skill 中的 IP 或源码路径。

最少需要一个测试工作目录，以及可访问的模型 endpoint 和模型名称。已有模型服务优先复用。新部署模型时使用客户指定的模型/精度/并行配置，不自动下载大模型或替换现有服务。

先运行：

```bash
python3 <SKILL_DIR>/scripts/benchmark.py doctor --workspace <CUSTOMER_WORKDIR>
```

医生检查不改变系统配置。首版执行器支持 Linux x86_64/KVM；Windows/macOS 控制端通过客户指定的 Linux 测试主机执行。

## 2. 固化可评审的实验方案

读取 [实验设计](references/experiment-design.md)，明确问题：比较硬件、找可用并发范围、评估隔离开销，或测实际业务吞吐。根据客户需求选择工作负载、点集、重复次数和计时模式。

默认基线：`algorithm_lab_sorting_bugfix`、每 VM 2 vCPU / 2048 MiB、库线程数 1、temperature 0、max tokens 1024、task/command timeout 600/120 秒。没有指定点集时可从 `1,2,4,8,32,64,128` 提案；资源预算必须先通过，不能静默降低内存或删改客户指定的点。

先报出具体计划，再执行已授权的测试。smoke 为 c1 一次；正式比较建议至少 3 次重复，并记录顺序、cache 状态、CPU 绑定和 NUMA 策略。客户指定单轮时遵循并标明统计限制。

CPU/内存超配、VM 启动节流和 cold boot 都属于实验因素。当前执行器仅实现 cold-boot 单波；ready-pool、steady-state、snapshot 模式需另行实现，不能仅改标签宣称已支持。

## 3. 在客户工作目录准备环境

```bash
python3 <SKILL_DIR>/scripts/benchmark.py prepare --workspace <CUSTOMER_WORKDIR>
```

该步骤从 skill 内的 assets 复制 runtime、创建隔离 Python 环境、复用或下载 Firecracker、准备兼容 guest kernel、构建 Ubuntu guest，并预装完整 mini-swe-agent、依赖和工作负载。大镜像与运行数据只落入客户工作目录，不写回 skill 包。

固定并记录 VMM/kernel/rootfs/source/dependency 标识。客户已提供兼容 kernel 时使用 `--kernel-file`。改变 RAM 不必重建镜像；改变 guest 源码/依赖需要重建。若缺失 Docker、KVM 权限或主机包，按部署文档解决具体依赖后继续，不得回退为宿主机 agent。

## 4. 先证明测试链路正确

```bash
python3 <SKILL_DIR>/scripts/benchmark.py smoke \
  --workspace <CUSTOMER_WORKDIR> \
  --base-url <MODEL_ENDPOINT> --model <SERVED_MODEL>
```

鉴权通过临时 `API_KEY` 环境变量提供，不写入命令参数、镜像或结果。核验 guest boot ID、agent/model client PID、guest 验收、trajectory、token usage、client TTFT/TPOT、guest/VMM/host 采样以及清理结果。

任务提交或打印成功标志不足以证明修复成功，必须由 harness 验收。smoke 失败先诊断；不能跳过失败后直接把大并发结果当作有效测试。

## 5. 执行受控实验

```bash
python3 <SKILL_DIR>/scripts/benchmark.py run \
  --workspace <CUSTOMER_WORKDIR> \
  --base-url <MODEL_ENDPOINT> --model <SERVED_MODEL> \
  --concurrency-levels 1,2,4,8,32,64,128 --repeats 3 --dry-run
```

核对计划后删除 `--dry-run` 执行。`--vm-cpuset`、`--model-cpuset`、`--memory-mib` 等可覆盖基线；CPU 编号必须来自客户拓扑，不复用作者机器编号。内存超配使用显式 `--allow-memory-overcommit` 并记录。

正式测试不临时安装包、改变模型或优化目标。重试不能覆盖失败 attempt；collector 缺失与任务失败分别报告。出现宿主机 OOM、模型崩溃或系统性 provisioning failure 时停止扩大并发，保留已完成证据再诊断。

## 6. 分层采集与校验

读取 [指标定义](references/metrics.md)。保留原始样本与汇总表，至少覆盖：

- 每任务 LLM：输入/输出 token、调用次数、请求时长、TTFT、TPOT、重试/错误。
- 生命周期：完整任务 E2E、VM 准备/启动排队、agent、工具、验收、导出与销毁时间。
- 每 VM：guest CPU/RAM、agent RSS；宿主机对应 VMM PID 的 CPU/RSS。
- 宿主机：CPU、RAM、可用的内存带宽、每 GPU 的利用率/显存/功耗等。
- 模型服务：可访问时采集队列、KV cache、token/request counter；远程模型的 GPU 需要在模型服务器另行采集。

核心结果必须有 guest 执行和任务验收证据。NVIDIA GPU、vLLM Prometheus、AMDuProf/Intel uncore 等增强采集器依实际支持情况启用；不可用写明原因，不能用 0 冒充数据。首版 GPU 执行采集适配 NVIDIA；其他 GPU 需匹配采集器并验证单位，不宣称自动支持。

自动汇总位于各次 run 的 `validated/`。核对请求点集、任务数、唯一 guest boot ID、配置资源、实际峰值并发、原始文件和 collector 状态。不要修改历史数据使其“匹配”新配置。

## 7. 分析和交付

以成功任务/秒和成功率作为主要有效工作指标，结合完整任务 E2E、CPU/GPU 利用率、token 和阶段耗时解释瓶颈。利用率升高不等于效率升高，也不能凭一条曲线断言唯一瓶颈。

交付环境/实验 manifest、逐次与汇总 CSV、逐任务/请求数据、原始采样、失败原因、清理状态和可复现命令。说明测量缺项、重复次数和跨机器控制条件；没有能量或 matched window 时不报告虚假的 tasks/J。

只清理本次创建的 VM、relay、临时磁盘和 collector。已有客户服务不擅自停止；本次新建服务按约定收尾。此 skill 不自动上传客户数据、push 仓库或改变全局系统配置。
