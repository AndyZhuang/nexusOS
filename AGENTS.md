# NexusOS — 项目约定

AI 原生的个人 AI 工作站系统层。**rootless-first、零第三方依赖**。

## 硬约束（本项目的地基，改代码前先确认没违反）

1. **只用 Python 3.11+ 标准库**。不许引入第三方依赖 —— 构建机无外网，pip/apt 都不可用。
   需要新能力时先找 stdlib；实在没有就写实现（参考 `mcp.py` 手写的 JSON-RPC）。
2. **不写 /etc、不建 system unit、不装包、不 mount**。目标机 `sudo` 需要密码。
   - 配置 → `$XDG_CONFIG_HOME/nexus`
   - 数据 → `$XDG_DATA_HOME/nexus`
   - 服务 → `systemd --user`（靠 linger 常驻）
   - 分层 → symlink 原子交换，不用 kernel overlayfs
3. **路径不硬编码**。一律走 `config.data_home()` / `config.config_home()`，
   环境变量 `NEXUS_DATA_HOME` / `NEXUS_CONFIG_HOME` 可覆盖。

## 代码规范

- 中文注释与文档字符串（用户是中文使用者，输出也是中文）。
- 模块职责边界清晰：`registry`（真相源）/ `broker`（仲裁）/ `gateway`（门面）/
  `sandbox`（边界）/ `mcp`（工具）/ `agent`（宿主）/ `events`（总线）/ `doctor`（事实快照）。
- **注册表是唯一真相源**。任何地方不要另存一份模型/后端配置。
- **常驻服务要热重载配置**：用 `registry.current_registry()`（按 mtime 缓存），
  不要在 `__init__` 里把 Registry 实例快照下来。
- 新增工具调用一律走 `mcp.py` 的 `ToolRegistry`，并在 `policy.toml` 里声明约束。

## 测试

```bash
bash tests/selftest.sh        # 20 项确定性自检（mock 适配器，秒级）
bash install/nexus-bootstrap  # 幂等安装 + 重启用户单元
```

改了 `src/nexus/` 之后必须重跑 bootstrap —— 运行的是 `~/.local/share/nexus/base/lib/` 里的副本，
不是源码树。

## 已知陷阱（别再踩）

- `dumps_toml` 自己写的，只支持 dict / list[dict] / 标量三类；字符串列表会被当标量处理。
- Ollama 的 `messages[].content` 必须是 **string**，不能是 list。
- 小模型（如 `dolphin3:8b`）**不支持原生 tools**，需走 `agent.py` 的 prompt-based 回退路径。
- 模型输出里常混入零宽字符（U+200B），解析前必须清洗（见 `_INVISIBLE`）。
- 模型给的工具名常是命名空间（`gpu` 而非 `gpu.models`），需要 `resolve_tool_name` 补全。
- 评估模型显存适配时要**扣除它自己的驻留占用**，否则会误判为 partial。
- 从 `/mnt/e`（drvfs）加载 GGUF 权重极慢（4.9GB 需 125s），先 `nexus models stage` 到 ext4（44s）。

## 交付物

- `docs/00-DESIGN-REVIEW.md` —— 对原 v0.1 设计的评审（12 个硬伤）
- `docs/01-NexusOS-Design-v0.2.md` —— 设计定稿
- `docs/02-DELIVERY-REPORT.md` —— 实装验证报告
