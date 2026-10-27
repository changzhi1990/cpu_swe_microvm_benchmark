# 指标和结果契约

## 数据层次

| 层 | 指标与单位 | 作用域 |
| --- | --- | --- |
| LLM client | prompt/completion/total tokens、调用/错误/重试次数、请求时长、TTFT、TPOT，秒 | guest 内客户端；逐任务/逐逻辑请求 |
| vLLM service | running/waiting、KV cache fraction、token/request/preemption counter delta、可用的 latency sum/count | 配置的共享模型 endpoint |
| 完整任务 | E2E p50/p90/p95/p99、queue、boot、agent、LLM/bash、validation、export、destroy，秒 | 生命周期边界见下文 |
| guest | CPU 利用率、RAM MiB、agent PID 的 core-percent/RSS/线程数 | 每个 VM 的完整 worker 窗口 |
| VMM | 宿主机 PID 的 core-percent、RSS MiB、线程数 | 每个 VM，含 boot 至 export |
| host CPU/RAM | 全机与所分配 cpuset 的 CPU%、sysfs 当前 MHz、RAM MiB | 物理宿主机 |
| memory bandwidth | read/write/total p90/max，采集器 GB/s | 宿主机物理内存控制器，非单 VM |
| GPU | 每卡 util%、controller busy%、显存 MiB、瓦特、SM/memory MHz、温度 | 共享 GPU / 模型服务，不是单 VM |

不要把 guest core-percent 与整体 CPU% 混用。guest CPU% 是跨其 vCPU 的 0–100%；2 vCPU 的 guest 中单线程排序通常约占 50%。进程 core-percent 可超过 100%。SMT 逻辑 CPU 不等同于独立物理核。

`host_cpu_util_percent_avg` 为整机 CPU，`host_allocated_cpu_util_percent_avg` 为分配集合。Linux 的 guest 计时已包含在 user/nice 中，不能重复求和；随包 runtime 仅累计前八项。短窗旧字段与连续窗口字段仍不应混合比较。所有 GPU 的来源默认标记为 benchmark host；模型在远程时需要另一份 model-host 采样。

## 文件

- `global_metrics.csv`：每 workload / 配置并发一行；CPU/GPU/vLLM 各分类表保留对应层字段。
- `llm_calls.csv`：每个逻辑模型请求；使用 completed 请求计算 client latency 分位数。
- `latency_metrics.csv`、`guest_metrics.csv`、`vmm_metrics.csv`：每任务一行。
- 点目录 `host_metrics.jsonl`、`vllm_service_metrics.jsonl`、`host_gpu_timeseries.csv`：共享硬件与 endpoint 原始时间序列。
- 每任务 `guest_artifacts/attempt/guest_metrics.jsonl` 与 `vmm_metrics.jsonl`：完整 guest / VMM 原始采样。
- `guest_artifacts/attempt/trajectory.json`、`agent.log`、guest `run_result.json`：完整 agent 的执行证据；宿主机 result 添加编排 metadata。
- `amd_pcm/`、`dcgm/`：硬件 collector 原始日志；`collection_status.json` 记录来源和可用性。
- 汇总脚本写入独立的 `validated/`，不覆盖输入 run 或原始样本。

## 时间、统计与比较

`task_e2e_seconds` 从宿主机 worker 入口到 guest 验收结束，包含启动排队与 VM 准备；export/destroy 单列。`guest_task_e2e_seconds` 只含 guest worker 窗口。旧 `E2E_p90_seconds` 是 agent loop 窗口，不是完整任务 E2E。

`batch_wall_time_seconds` 在全部任务结束后、collector shutdown 前截止，以 `timing_schema` 标识。采集器实际首尾样本时间另有字段；final read 可能略跨出 batch 边界。使用匹配窗口做能效分析。

默认 guest/VMM 每 0.5 秒、host/service 每 1 秒采样。瞬时峰值可能漏采，guest 原始单调时间仅在该 guest 内比较，跨 guest 对齐使用 wall timestamp。跨 VM 的 `*_mean_across_tasks` 是任务均值的平均，不是全局 CPU 时间加权平均。

GPU `utilization.memory` 是控制器繁忙百分比，不是 GB/s；vLLM cache `*_perc` 保留 0–1 fraction。Counter 用首尾差值；reset 记 null。模型 endpoint 若被其他客户端使用，service counter 会包含其流量；不能强行与本次 client token 数相等。

GPU 瓦特不能当 CPU package/system 瓦特。没有能量数据时不要计算 token/J；`tokens / average_watts` 也不是能效。没有 AVT 样本时不把 sysfs 当前 MHz 冒充 effective frequency。

对比相同内存、vCPU、CPU 集合、模型参数、镜像/依赖版本和 cache 策略；冷启动排队可能主导高并发 E2E。一次单波测试只能说明本次结果，不能断言统计显著、唯一瓶颈或生产最优并发。
