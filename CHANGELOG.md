# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.3.0] — 2026-10-23

权限体系重构 + 新手引导。用户实测反馈"权限限制得太死"，这一版把权限做成
**可分档调节的统一模型**，并让"双层边界"从设计文档变成运行时事实。

### 新增

- **权限档位**（`src/nexus/permissions.py`）：一个 tier 同时驱动沙箱层与工具策略层。
  - `strict` —— 仅 nexus 数据区可写；系统目录与家目录只读。共享机器 / 多租户。
  - `standard` —— **新默认**。家目录完全可写（含项目代码），/tmp 可写，系统目录只读。
  - `open` —— 再放开 `/etc` `/opt` `/srv` `/usr/local`。
  - `off` —— 不套任何 systemd 沙箱（仍留审计记录）。
  - `nexus permissions` 查看 / `nexus permissions set <档位>` 切换 / `nexus permissions list` 列全部档位。
- **沙箱子进程机制**（`src/nexus/agent.py`）：agent 进程整体通过 `systemd-run --user`
  在档位对应的内核沙箱里运行。此前 local 适配器的工具调用在主进程内直接执行，
  `sandbox_tier` 只是个记录参数 —— "双层边界"实际只有策略层一层。
- **新手引导**（`src/nexus/tutorial.py`）：`nexus` 无参数即进入引导，6 步**真的执行命令
  并展示真实输出**；进度记在 `state/tutorial.json`，二次进入改为速查表；`nexus tutorial --reset` 重来。

### 修复

- 危险命令黑名单（`rm -rf /`、`mkfs`、`dd` 写裸盘等）在所有档位都生效，
  且**显式清空** `denied_commands = []` 才解除（原先 `or` 兜底导致空列表无法生效）。
- `policy.toml` 里写死的 `read_roots` / `write_roots` 会架空档位 —— 现在
  `nexus permissions set` 会主动清除它们并明确告知。
- 去掉 `PrivateDevices=yes`：该选项会藏掉 `/dev/dxg`，导致沙箱内 GPU 查询失效。
  改为由其余内核级防护（ProtectSystem / ProtectHome / NoNewPrivileges 等）承担。

### 变更

- 默认档位 `strict` → `standard`（agent 现在能改你自己的项目代码）。
- 新增迁移 `1753500000.sh`：把既有 `policy.toml` 从"写死路径根"迁移为"档位驱动"。
- `nexus status` 显示实时版本号与当前权限档位（此前版本号是硬编码的 `0.2.0`）。

### 已知限制

- `standard` / `open` 档下 agent 可写你的家目录（含 `~/.ssh`），并可发起网络连接
  （本地推理后端需要 `AF_INET`）。这是"让它真的能干活"的代价，共享机器请用 `strict`。
- `cli` 适配器需要外网（云端 API），当前环境下只有 `local` 可用。
- 无 root 时无法使用 kernel overlayfs 后端与 systemd 系统单元。

## [0.2.0] — 2026-10-23

首个可发布版本。AI 原生系统层的第一版完整实现。

### 新增

- **Layer 0 基座**：分层管理器（symlink / overlayfs 双后端）、两级配置合并（系统层 + 用户层）、
  幂等安装器 `nexus-bootstrap`、5 个 systemd --user 单元。
- **Layer 1 资源仲裁**：模型注册表（唯一真相源）、显存仲裁器（独占加载 + 预算 + 降级建议）、
  OpenAI 兼容 AI 网关、模型 stage（drvfs → ext4 加速）。
- **Layer 2 Agent 宿主**：适配器 SPI（`local` / `cli` / `mock`）、systemd 沙箱三层信任边界、
  MCP server（stdlib JSON-RPC，26 个工具）、审计 JSONL、会话与记忆落盘。
- **Layer 3 事件总线**：journald 订阅 + 声明式规则引擎（5 条内置规则）、
  事实快照构建器 → agent 派发 → 诊断归档、300 秒去抖、规则作用域隔离。
- **Layer 4 对话式控制面**：`nexus` 单命令入口，自然语言直接驱动系统状态变更。
- **Layer 5 MCP**：JSON-RPC 2.0 over stdio，工具命名空间 fs / shell / git / sysd / gpu / journal / nexus。
- **发布工程**：版本与通道体系（stable / rc / edge / dev）、迁移机制（`migrations/` 时间戳脚本）、
  `nexus update` 单一更新入口（空间检查 → 快照 → 取产物 → 迁移 → 换基底 → 重启）、
  `bin/nexus-release` 发布流水线、tarball 与 deb 双产物、单文件安装器、CI 与 Release workflow。

### 设计变更（相对原始 v0.1 设计）

- overlayfs 根 + tmpfs 上层 → **symlink 原子交换**，upper 落盘（解决 OOM 与丢改动二选一）。
- 双后端共存 → **注册表唯一真相源 + 独占加载 + 显存预算**（解决单卡显存翻倍）。
- 自研 Go/Rust harness → **适配器 SPI + 宿主层**，agent 本体可换（对齐 Omarchy 的复用范式）。
- 软约束不阻断 → **systemd 内核级沙箱 + MCP 策略层双边界**（消除 Omarchy 4.0 同款漏洞形态）。
- 重建 `/ro` 与 `apt upgrade` 并存 → **mutable-first**，分层只负责回厂。

### 已知限制

- `cli` 适配器需要外网（云端 API），当前环境下只有 `local` 可用。
- 无 root 时无法使用 kernel overlayfs 后端与 systemd 系统单元。
- 部分小模型不支持原生 function-calling，走 prompt-based 回退路径。
