# 客户部署与执行

## 支持与前提

首版执行器验证于 Linux x86_64、KVM 和 Firecracker 1.16.0。需要可读写 `/dev/kvm`、Python 3.10+ 与 venv、Docker daemon 可用，以及 `mkfs.ext4`、mount/chroot 工具和 sudo。需要下载 Python/Ubuntu 包、guest kernel；已有网络策略或离线镜像时先配置其镜像源。Windows/macOS 作为控制端，真正 VM 测试在客户指定的 Linux 主机执行。

当前 launcher 没有 jailer，仅用于可信 benchmark 代码。它不把 VM 隔离等同于完整多租户生产安全配置。当前模型字节转发支持 HTTP；HTTPS-only 服务需要先实现并验证相应 transport，不能静默禁用 TLS 校验。

官方背景：[Firecracker requirements](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)、[vsock](https://github.com/firecracker-microvm/firecracker/blob/main/docs/vsock.md)。

## 无私有 checkout 的准备流程

`benchmark.py prepare` 从本 skill 的 `assets/runtime` 建立新的客户 workspace，创建 `.venv`、安装 host dependencies、复用或获取 VMM、准备 kernel，并用随包 Dockerfile 构建 guest。完整 mini-swe-agent 2.4.5 源码与 MIT 许可证已在 vendor 中，不需要作者机器或额外私有仓库。

已验证组合为 Ubuntu 26.10 rootfs + Linux 6.1.188 guest kernel。VMM、host OS 和 guest OS 是独立版本。kernel 下载使用随包 SHA256 校验；VMM release 使用固定版本和 HTTPS，下载/复用后记录实际 SHA256。安装在 workspace/tools，不覆写系统 VMM。

生成的 `prepared.json`、`environment.json`、`host-requirements.txt`、guest rootfs SHA256 和 image ID 是复现实验的一部分。跨机器公平比较时分发同一预制 image/kernel，而不是各自在不同日期 apt install 后假定软件一致。guest source/dependency 改动后新建 workspace/image，避免覆盖旧结果。

## 使用入口

定义 `SKILL_DIR` 为客户安装的 skill 文件夹，`WORKDIR` 为新的测试工作目录。模型 endpoint/名称由客户服务发现或客户提供；不要复制作者环境的 endpoint、模型名称、CPU 编号或口令。

```bash
python3 "$SKILL_DIR/scripts/benchmark.py" doctor --workspace "$WORKDIR"
python3 "$SKILL_DIR/scripts/benchmark.py" prepare --workspace "$WORKDIR"
python3 "$SKILL_DIR/scripts/benchmark.py" smoke --workspace "$WORKDIR" \
  --base-url "$BASE_URL" --model "$MODEL"
python3 "$SKILL_DIR/scripts/benchmark.py" run --workspace "$WORKDIR" \
  --base-url "$BASE_URL" --model "$MODEL" \
  --concurrency-levels 1,2,4,8,32,64,128 --repeats 3 --dry-run
```

先审阅计划，删除 dry-run 才执行。客户已经授权具体测试时不重复索取同一授权；如果请求缺少关键参数或预算不能满足，指出具体缺口再处理。

设置临时 `API_KEY` 环境变量提供鉴权；profile/argv 不记录其值，trajectory 对其脱敏。CPU 集合通过 `--vm-cpuset` 和可选 `--model-cpuset` 传入，不写死编号；脚本检查声明的集合及 SMT overlap，但不会自动重绑现有模型进程，实际配置需核对。

准备会调用 sudo 进行 rootfs 文件操作；硬件带宽工具可能需要额外权限。可在当前交互 shell 建立 sudo 授权；独立 session 的 collector 可用临时 `AMDUPROFPCM_SUDO_PASSWORD`，不要写文件。`AMDUPROFPCM_PATH` 指向客户已合法安装的工具，未提供时尝试 PATH。Intel uncore 可用时使用 perf 路径。缺失增强采集器不会被伪装为正常的零值。

## 模型与 GPU 的位置

本机模型：host GPU 时间线与此共享模型服务相关，但仍不能精确归因到单个 VM。远程模型：`/metrics` 可从 endpoint 采集；远程 GPU 必须在模型节点单独采样并使用时间戳关联。没有访问权限时明确标记模型 GPU 不可用，不把本机 GPU 冒充远程模型 GPU。

当前 GPU query 适配 NVIDIA-SMI/DCGM；AMD/其他 GPU 需要客户环境对应适配器。CPU、client LLM 和 guest 测试不依赖一定存在 NVIDIA GPU。vLLM Prometheus 是增强能力；使用非 vLLM 兼容服务时可缺失，若实验要求它则传 `--require-service-metrics`。

## 客户输出与收尾

每次正式运行创建独立 results 目录；smoke、正式 trial、不同 RAM/model/image 不能互相覆盖。`benchmark.log`、`run_profile.json`、阶段日志和每个 guest 的代码/trajectory 都在 workspace。`validated/` 保存核验后的分类表，原始文件不改写。

采集器或模型挂掉时保留证据，停止扩大压力并诊断。只清理本次拥有的 VMM、Unix socket、临时磁盘和 collector；模型服务原本存在时不擅自停止。prepare 的依赖失败是具体阻塞项，不得通过改成 native 模式假装完成测试。
