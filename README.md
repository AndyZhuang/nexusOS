# NexusOS 0.2

> AI 原生的个人 AI 工作站系统层 —— rootless-first，零第三方依赖。
> 设计定稿：`docs/01-NexusOS-Design-v0.2.md` ｜ 设计评审：`docs/00-DESIGN-REVIEW.md`

---

## 这是什么

一个**能真正跑起来**的 AI 原生操作系统层。不是"Linux 上装了 Ollama"，而是：

- **系统事件反向驱动 agent** —— 崩溃 / GPU OOM / 单元失败 / 磁盘满 → 自动唤醒 agent 采集事实快照 → 给出根因与修复步骤
- **对话式控制面** —— `nexus "把显存压到 8G 以下"` 让 agent 真的去改系统状态，而不是给建议
- **资源仲裁** —— 模型注册表是唯一真相源，显存预算内独占加载，超预算拒绝并给可执行建议
- **内核级信任边界** —— agent 跑在 systemd 沙箱里（只读系统目录 + 仅 nexus 数据区可写），不是提示词级的"软约束"

设计对标 Omarchy 的**操作系统哲学与系统级集成**，而不是它的包管理。

---

## 30 秒上手

```bash
git clone <repo> && cd nexusOS
bash install/nexus-bootstrap        # 幂等安装，无需 root
export PATH="$HOME/.local/bin:$PATH"

nexus status                 # 系统总览
nexus gpu                    # 显存 + 模型适配判定
nexus models list            # 模型注册表
nexus mcp test               # MCP 协议自检（26 个工具）
nexus events list            # 事件规则表
nexus events test crash      # 事件 → 事实快照 → agent 诊断 全链路
nexus "现在显卡占了多少显存？"  # 对话式控制面
```

---

## 架构

```
Layer 4  nexus CLI ──────── 对话式控制面（Omarchy 的 omarchy 命令位）
Layer 3  event-router ───── 系统事件 → 规则 → 事实快照 → agent → 诊断归档  ★差异化核心
Layer 2  harness-host ───── agent 宿主：沙箱 / MCP 供给 / 权限 / 审计 / 记忆
                              适配器 local(Ollama) · cli(claude/opencode) · mock
Layer 1  资源仲裁 ───────── model-registry · gpu-broker · ai-gateway(OpenAI 兼容)
Layer 0  基座 ───────────── 分层管理(symlink/overlayfs) · 两级配置 · systemd --user 栈
```

---

## 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 部署形态 | **rootless-first**（`systemd --user` + linger） | 实测 sudo 需密码 → 无法装包/mount/建 system unit。rootless 是让整套东西在任何机器上免 root 部署的唯一现实路径 |
| 分层 | **symlink 原子交换**（默认）/ overlayfs（root 可选） | 无 root 不能 mount。symlink swap 提供等价语义：可变层 + 一键回厂，且 upper 落盘而非 tmpfs |
| 信任边界 | **systemd 沙箱指令** | 无网装不了 bubblewrap。`ProtectSystem=strict` + `ReadWritePaths=` 零依赖 |
| Agent | **不自研 harness**，只做宿主层 | Omarchy 的成功范式是复用（"pre-wired, lazy-loaded"）。自研是几周起步且无社区兜底的时间黑洞 |
| 模型管理 | **注册表唯一真相源 + 显存仲裁** | v0.1 的"双后端共存"会让同一模型在两个后端各加载一份 → 显存翻倍 → 单卡必 OOM |
| 依赖 | **纯 Python stdlib** | 无外网 → 无法 pip/apt。`tomllib` 3.11+ 已足够 |

### 显存仲裁示例（本机实测）

```
模型                                        显存MB   适配      判定依据
dolphin3:8b                               4692   gpu      显存充足（余 8084MB > 需 4692MB）
dolphincoder:15b-starcoder2-q4_K_M        9403   partial  显存不足，将部分层卸载到 CPU
richardyoung/qwen3-14b-abliterated:Q5_K_M 10027  partial  显存不足，将部分层卸载到 CPU
qwen3.6:27b                               16057  cpu      显存严重不足，只能 CPU 推理 → 拒绝加载
```

---

## 命令参考

```
nexus status                    系统总览（分层/配置来源/GPU/后端/单元/MCP 工具集）
nexus doctor [--since 1h]       主动诊断，输出结构化事实快照
nexus doctor --agent            附带 agent 分析
nexus gpu                       显存快照 + 模型适配判定
nexus models list|load|unload   模型注册表与加载/卸载
nexus models route <name>       路由决策可视化
nexus models discover           从后端发现模型
nexus models stage <name>       把模型从共享 store 复制到本地 ext4（加载提速 3 倍）
nexus events list|test <rule>   事件规则表 / 人工触发一条规则走完整诊断链路
nexus events follow             跟随 journald 实时诊断
nexus agent "<任务>"            直接调用 agent（--adapter local|cli|mock）
nexus "<自然语言>"               对话式控制面
nexus mcp list|test             MCP 工具清单 / 协议自检
nexus audit                     审计统计
nexus logs audit|events|router  日志
nexus reset [--dry-run]         回到底层基底
nexus explain                   展示当前信任边界与实际注入的 system prompt
nexus gateway --port 8080       前台启动 OpenAI 兼容网关
```

---

## 目录规范

```
$XDG_DATA_HOME/nexus/
├── base/            只读基底（代码 + 入口脚本 + 单元模板）
├── live/            可变层
├── current -> live  原子可换链接（回厂时整体替换）
├── ollama-models/   本地 ext4 权重仓库（stage 后的快速加载区）
├── agent/{sessions,memory}/
├── incidents/       事件诊断归档（json + md）
├── log/{audit,events,gateway}/
└── state/

$XDG_CONFIG_HOME/nexus/
├── models.toml      模型注册表（系统层 /etc/nexus 可提供只读默认值）
├── events.toml      事件规则
├── policy.toml      工具权限策略（allow/ask/deny）
├── prompts/         system prompt 注入
```

---

## 信任边界

```
默认（tier=strict）
  ✓ ProtectSystem=strict     系统目录只读
  ✓ ProtectHome=read-only    家目录只读
  ✓ ReadWritePaths=          仅 nexus 数据区/配置区可写
  ✓ PrivateTmp / PrivateDevices / NoNewPrivileges
  ✓ 危险命令不阻断，但在沙箱内没有 root 能力
  ✓ 全程审计 JSONL

放开（显式、可审计）
  nexus --tier off           → 记录为 agent.run.unsafe
  policy.toml allow_restart  → 单元重启权限
```

MCP 工具层是**第二道**边界：`fs.write` 到 `/etc` 会被策略直接拒绝，与沙箱无关。

---

## 环境要求与实测底账

| 项 | 要求 |
|---|---|
| 发行版 | Ubuntu 24.04（WSL2 实测通过） |
| Python | 3.11+（需 `tomllib`），**无第三方依赖** |
| init | systemd（`systemd --user` + linger） |
| GPU | NVIDIA + 驱动；WSL2 下 `/dev/dxg` 直通可用 |
| 推理后端 | Ollama（`/usr/local/bin/ollama`） |

**已知限制（诚实登记）**

| 限制 | 影响 | 缓解 |
|---|---|---|
| 无外网 | `cli` 适配器不可用（claude/opencode 需云端 API） | 代理恢复后自动探测切换 |
| 无 sudo | 不能装包 / 不能 kernel overlayfs / 不能 system unit | rootless 架构已规避；裸机 profile 用 root |
| 无 `systemd-coredump` | 崩溃元数据靠 journald 字段 | 裸机装 `systemd-coredump` 即可获得完整 coredump |
| drvfs 加载慢 | 从 `/mnt/e` 加载 4.9GB 权重需 125s | `nexus models stage` 复制到 ext4 → 44s |
| 小模型不支持原生 tools | 部分 GGUF 无 function-calling 模板 | 适配器自动探测并退回 prompt-based 工具循环 |
| Windows 侧占显存 | 本机实测常驻占用 8–14GB | broker 实时核算，给出降级建议 |

---

## 目录结构

```
nexusOS/
├── docs/
│   ├── 00-DESIGN-REVIEW.md      对 v0.1 的评审：12 个硬伤 + 证据 + 修法
│   └── 01-NexusOS-Design-v0.2.md 设计定稿
├── install/nexus-bootstrap      幂等安装器
├── src/nexus/
│   ├── config.py    路径、两级配置合并、TOML 读写
│   ├── layering.py  分层管理（symlink / overlayfs 双后端）
│   ├── registry.py  模型注册表（唯一真相源）
│   ├── broker.py    显存仲裁 + 守护进程 + stage
│   ├── gateway.py   OpenAI 兼容网关
│   ├── sandbox.py   systemd 沙箱
│   ├── mcp.py       MCP server（stdlib JSON-RPC）+ 26 个工具
│   ├── agent.py     适配器 SPI：local / cli / mock
│   ├── events.py    事件总线（规则引擎 + 诊断派发）
│   ├── doctor.py    事实快照构建器
│   ├── audit.py     审计
│   └── cli.py       对话式控制面
└── systemd/nexus-*.service      systemd --user 单元
```

---

## 与 v0.1 的关系

v0.1 的**分区思路**、**保留 systemd 的判断**、**取舍的诚实度**被完整继承。
v0.1 的**双后端共存**、**自研 harness**、**软约束权限**、**overlay+tmpfs** 被推翻，
理由逐条记录在 `docs/00-DESIGN-REVIEW.md` §0.3。
