# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
