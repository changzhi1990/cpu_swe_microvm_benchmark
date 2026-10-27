# 客户实验设计

## 先确定问题

| 目标 | 固定项 | 自变量 | 首要观察 |
| --- | --- | --- | --- |
| 比较 CPU/服务器 | 同 rootfs/kernel/agent/workload/model/精度/参数/cache/资源策略 | 服务器或 CPU | 相同成功率下的成功任务/s、E2E、工具/验收耗时 |
| 找并发范围 | 同一软硬件和单 VM 规格 | VM/agent 配置并发 | 吞吐、tail latency、失败、queue、resource pressure |
| 评估隔离成本 | 同任务、模型、guest/host 依赖尽量一致 | native 与 microVM 或明确的 VM 生命周期模式 | 准备成本与运行成本分别比较 |
| 评估业务工作负载 | 固定仓库版本、任务集、评分方式 | 客户任务/资源场景 | 正确完成的业务任务及其成本 |

写入 manifest：硬件/拓扑、模型服务位置与版本/精度/TP/context limit、agent/source/image/kernel 哈希、workload/test 版本、并发、vCPU、RAM、CPU/NUMA、库线程数、超配、超时/token/step budget、采样间隔、repeat/顺序、prefix cache/warmup、时间窗口。

## 当前执行器的模式

实现的是本地镜像 cold boot、每点 N 个 attempt 的单波。启动并发上限默认 4，因此 agent 开始时间可以错开；记录配置并发、实际 guest/VMM 峰值，以及模型请求峰值。三者不是同一个指标。

每任务重新复制工作负载并新建 VM。模型服务可以持续运行，但 warmup 和 prefix cache 策略必须明确：预热请求不计入正式任务，不能在点间悄悄 reset cache。执行器按传入点序运行；随机化时显式生成并记录每轮顺序，不宣称脚本自动随机化。

cold boot、ready VM 和持续补充任务的 steady-state 回答不同问题。后两种尚未实现。不能用当前单波吞吐推断持续到达流量下的 SLA 或无限期稳态。

## 基线工作负载

随包包含 `algorithm_lab_sorting_bugfix` 和 `memory_lab_bandwidth_bugfix`。默认排序：固定 buggy 仓库、Python 冒泡排序、10000/20000 元素测试、源码修改约束和 harness pytest 验收。默认仅运行客户要求的 workload；不能将所有任务或点集偷偷混入。

客户自定义任务时，在复制到工作目录的 runtime 中添加 repo template 和 Workload 注册项，声明 task prompt、初始 commit/文件哈希、允许修改范围、独立验收命令、成功定义及预算，重建 guest 后先 c1 验证。当前入口不声称支持未经实现的任意 dataset manifest 格式。

相同 bug 的复制任务适合控制并发因素，但不代表多样化 SWE 任务解决能力。不能以该任务的 100% 成功率声称模型在真实客户代码上同样成功。

## 资源与统计

基线每 VM 2 vCPU、2048 MiB。严格 RAM 预算使用启动模型后的 MemAvailable，额外留 16 GiB 宿主机余量。配置内存、guest MemTotal 和 host VMM RSS 必须分开；按需驻留不能当作无风险容量保证。显式记录 CPU/内存超配，不能自行改变客户点集。

CPU 绑定根据当前拓扑生成。共享同一物理核的 SMT siblings 会相互影响；模型 CPU 与 agent CPU 的隔离或共享策略在比较机器之间保持一致。sysfs MHz 不能冒充 effective frequency。

正式比较建议每场景至少 3 个独立 repeat，给出每轮结果、均值/标准差或置信区间以及请求/任务样本数。脚本保存每轮独立 run；跨轮统计由 agent 从各轮 validated 表计算。不要对 p99 的均值声称是合并请求的 p99，也不要在不同 task shape/model 配置间直接拼接分位数。

## 结果有效性

任务正确性与采集完整性分开判定。成功应同时具备 agent 提交、harness 验收通过和 guest 来源证明；基础修复任务按约束检查是否修改 tests。当前评测不是对恶意 agent 的防篡改隐藏评分服务，也不是 multi-tenant 安全认证。

保留失败的 denominator、token/耗时和失败阶段。基础设施失败、模型错误、超时、未提交、验收失败分别归类。重复运行产生新的 attempt/run，不覆盖失败后只发布成功样本。
