# NexusOS v0.2 · 设计定稿

> 基线：`NexusOS-Design-Developer.md` v0.1（个人开发者版）+ 设计评审
> 状态：**已锁定架构决策，待实现验证**
> 日期：2026-10-23

---

## 0. 决策记录

### 0.1 已拍板（用户决策）

| # | 决策 | 选择 |
|---|---|---|
| D1 | 落地载体 | **WSL2 当主力** —— 今天就真跑起来 |
| D2 | AI-native 主线 | **系统被 agent 驱动** + **对话式系统**，对标 Omarchy |
| D3 | 基底与升级 | **Ubuntu 24.04 + 可变系统 + 分层落盘** |
| D4 | Agent 来源 | **复用现成 Agent CLI，Nexus 只做系统集成层** |

### 0.2 自主决定（评审推导，用户可推翻）

| # | 决策 | 理由 |
|---|---|---|
| A1 | **rootless-first**：整个系统栈跑在 `systemd --user` 实例上 | 实测 sudo 需密码 → 无法装包/mount/建 system unit。而 `systemd --user` + linger 完全可用。rootless 不是妥协，是让整套东西在**任何**机器上免 root 部署的唯一现实路径 |
| A2 | 分层用 **symlink 原子交换** 代替内核 overlayfs（rootless profile） | 无 root 不能 mount。symlink swap 能提供**完全等价的用户可见语义**（可变层 + 一键回厂），且免 mount namespace 折腾。内核 overlayfs 保留为 root profile 的可选后端 |
| A3 | 信任边界 = **systemd 沙箱指令**，不是 bubblewrap | 无网装不了 bwrap；systemd 原生支持 `ProtectSystem=strict` / `NoNewPrivileges` / `ReadWritePaths` / `PrivateTmp` / `RestrictAddressFamilies`，零额外依赖且与本 OS 的哲学一致 |
| A4 | Agent **SPI + 多适配器**，不绑定单一 agent | `local`(Ollama) / `cli`(claude/opencode) / `mock`(确定性) 三个适配器。离线时 local 兜底，有网时 cli 接管。Omarchy 的 "pre-wired, lazy-loaded" 精神 |
| A5 | AI 层零第三方依赖（纯 Python stdlib） | 无外网 → 无法 pip/apt。stdlib + tomllib 已足够。**这与同项目 `hidlab.py` 的既有风格一致** |
| A6 | 模型目录直接挂 `/mnt/e/Ollama/models` | 复用 Windows 侧 Ollama store，**零拷贝零下载零重复占盘** |

### 0.3 对 v0.1 硬伤的修正对照

| v0.1 硬伤 | v0.2 修法 | 落在哪 |
|---|---|---|
| H1 overlay+tmpfs 上层 | 分层后端抽象：symlink（默认）/ overlayfs（root 可选），upper 落盘 | §3.1 |
| H2 双后端显存翻倍 | 模型注册表单一真相源 + 显存仲裁器，**独占加载**，超预算拒绝 | §3.2 |
| H3 自研 harness | Nexus 只做宿主层五件事；agent 本体由适配器供给 | §3.4 |
| H4 软约束不阻断 | systemd 沙箱为默认边界，`--unsafe` 显式放开 | §6 |
| H5 升级模型矛盾 | mutable-first，apt 唯一真相源，分层只管「回厂」 | §3.1 |
| H6 分区/目录打架 | 两级配置（system 默认 + user 覆盖）；软链由 `tmpfiles.d` 重建 | §4 |
| H7 vLLM 开机自启 | 后端按需拉起 + 闲置自动卸载；本地 profile 只跑 Ollama | §3.3 |
| H8 缺 agent 驱动主线 | **事件总线**成为一等公民：journald → 规则 → 结构化诊断 → agent | §3.5 |
| H9 裁剪不可执行 | 交付物改为「可复现构建脚本」，精简作为 profile 而非承诺 | §7 |
| H10 NVIDIA/DKMS | base profile 显式含 `linux-headers` + `dkms`，驱动选型二选一 | §7 |
| H11 networkd 埋雷 | 默认 NetworkManager，networkd 降级为 server profile | §7 |
| H12 Roadmap 无验收标准 | 每个 Phase 带**可执行验收命令** | §7 |

---

## 1. 运行环境真相（实测推导，非假设）

> 这一节是整个 v0.2 的地基。**所有架构决策必须能追溯到这张表。**

| 能力 | 实测 | 对设计的约束 |
|---|---|---|
| 发行版 | Ubuntu 24.04.3 LTS (WSL2) | 基底 ✅ 与 D3 一致 |
| init | `systemd` (PID1)，`/etc/wsl.conf` 已开 `systemd=true` | systemd ✅ |
| 用户级 systemd | `systemctl --user` → `running`；andy `linger=yes` | **A1 的地基**：用户单元可开机自启 |
| root 权限 | `sudo` **需密码** | ❌ 不能装包 / mount / 建 system unit |
| user namespace | `max_user_namespaces=63345`，`unshare --user --map-root-user` ✅ | 可做用户级隔离 |
| `systemd-run --user` | ✅ 可用 | **A3 的地基**：沙箱指令可用 |
| 内核 overlayfs | ❌ 需 root | A2：改用 symlink 分层 |
| Python | 3.12.3，`tomllib` ✅，**无 pip / ensurepip / numpy** | A5：纯 stdlib |
| 网络 | ❌ WSL 与 Windows 均无外网（代理 7897 关闭） | **云端 agent 不可用 → 本地 agent 为唯一可跑路径** |
| Ollama | 0.15.2 已装；system 单元在 11434（0 模型） | Nexus 自起用户实例于 11435 |
| 本地模型 | `/mnt/e/Ollama/models` 4 个（4.7 / 9.9 / 10.5 / 17.8 GB） | A6：共享 store，零下载 |
| GPU | RTX 3080 Laptop 16GB，驱动 596.47 / CUDA 13.2；`/dev/dxg` ✅ | 仲裁器靶场 |
| **显存实况** | **8361 / 16384 MiB 已被 Windows 侧占用，util 100%** | **可用仅 ~8GB → 27B 必然 CPU offload** |
| 缺失 | jq / bwrap / coredumpctl / docker / podman / vllm | 全部用 stdlib 或 systemd 原生替代 |
| 缺失 | systemd-coredump 工具 | 崩溃检测改用 journald 字段（`CODE_DUMPED`/`COREDUMP_*`） |

---

## 2. 分层架构

```
┌──────────────────────────────────────────────────────────────┐
│ Layer 4  nexus CLI  —— 对话式控制面（Omarchy 的 omarchy 命令位）│
│   nexus "把显存压到 8G 以下"  →  agent → 工具调用 → 真实执行  │
├──────────────────────────────────────────────────────────────┤
│ Layer 3  nexus-event-router  —— 系统事件反向驱动 agent        │
│   journald → 规则匹配 → 结构化诊断上下文 → agent → 处置建议   │
│   规则: 崩溃 / GPU OOM / 磁盘满 / 单元失败 / 内核 Oops        │
├──────────────────────────────────────────────────────────────┤
│ Layer 2  nexus-harness-host  —— agent 宿主（不自研 harness）  │
│   沙箱启动 / MCP server 供给 / 权限策略 / 审计 / 会话落盘     │
│   适配器: local(Ollama) · cli(claude/opencode) · mock        │
├──────────────────────────────────────────────────────────────┤
│ Layer 1  资源仲裁                                              │
│   nexus-model-registry  模型唯一真相源                        │
│   nexus-gpu-broker      显存预算 + 独占加载 + 卸载 + 降级建议  │
│   nexus-ai-gateway      OpenAI 兼容门面 + 后端路由 + 健康检查  │
├──────────────────────────────────────────────────────────────┤
│ Layer 0  基座                                                  │
│   分层管理器( symlink/overlayfs 双后端 ) · 配置两级合并         │
│   systemd --user 单元栈 · 审计日志                            │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. 关键机制设计

### 3.1 分层管理器（LayerManager）

**语义目标**：可变层（live）+ 只读基底（base）+ 原子回厂（reset）。

**rootless profile（默认，symlink 后端）**
```
/opt/nexus/base/            # 只读基底（bootstrap 安装）
/opt/nexus/live -> /data/nexus/live     # 符号链接，可原子替换
/data/nexus/live/           # 可变层实体
/data/nexus/.staging/       # 重建用暂存区
```
`nexus reset` 流程：
1. `base/` → 重建到 `/data/nexus/.staging/live`
2. 原子换链：`ln -sfn /data/nexus/.staging/live /opt/nexus/.live.new && mv -T /opt/nexus/.live.new /opt/nexus/live`
3. 清暂存

**root profile（可选，overlayfs 后端）**
```
lowerdir=/opt/nexus/base  upperdir=/data/nexus/upper  workdir=/data/nexus/work
```
`nexus reset` = `rm -rf upper/*`，瞬时完成。

**接口**（两种后端同一套 API）：
`install(payload)` · `status()` · `reset()` · `snapshot(name)` · `restore(name)`

### 3.2 模型注册表 + 显存仲裁

`~/.config/nexus/models.toml`（用户层）叠加 `/etc/nexus/models.toml`（系统层，只读默认）。

```toml
[[model]]
name        = "dolphin3:8b"
backend     = "ollama"
vram_mb     = 4900
params      = "8B"
quant       = "Q4_K_M"
priority    = 10
tags        = ["chat", "default", "fast"]
```

**仲裁算法**（`nexus models load <name>`）：
1. 读 `nvidia-smi` → `total_mb`, `used_mb`（含 Windows 侧占用）
2. `budget = total_mb - reserve_mb`（reserve 默认 1024）
3. 若 `used_mb + vram_mb > budget` → **拒绝加载**，并给出可执行建议：
   - 列出当前占用者（ollama / 其他后端 / 外部）
   - 建议卸载项及其释放量
   - 建议降级档（partial CPU offload 的可用参数）
4. 加载前**卸载其他已驻留模型**（独占语义）
5. 加载后回读实际占用，登记到 broker 状态

**显存不足的降级策略**（针对 27B / 17.8GB）：
- `fit = "gpu"` / `"partial"` / `"cpu_only"` 三档
- `partial` 档给出 Ollama 参数建议：`num_gpu` 偏小 + `OLLAMA_KV_CACHE_TYPE=q8_0`

### 3.3 AI 网关

OpenAI 兼容 HTTP 服务（stdlib `http.server`）：
- `POST /v1/chat/completions` → 按注册表路由
- `GET  /v1/models` → 合并注册表 + 各后端实时状态
- `GET  /healthz` · `GET /nexus/route`（路由决策可视化）

**路由**：`model` 名精确命中注册表 → `backend` 字段决定去 ollama / vllm / mock。
**后端健康**：周期性探活；不健康则按注册表 `fallback` 链降级。
**按需拉起**：vLLM 侧支持 `on_demand`（首次请求起，闲置自动卸载），默认**不常驻**。

### 3.4 Agent 宿主（nexus-harness-host）

**Nexus 不实现 agent 循环。** 它只做五件事：

| 职责 | 实现 |
|---|---|
| ① system prompt / skill 注入 | `~/.config/nexus/prompts/*.md` + `SKILL.md` 约定，注入到 agent 配置目录 |
| ② 权限策略 | `policy.toml` 三档 allow/ask/deny；deny 项由沙箱强制，不靠提示词 |
| ③ MCP server 供给 | 生成 MCP client 配置 JSON，指向 `nexus-mcp` |
| ④ 审计 | JSONL：每次工具调用、决策、执行结果、耗时、退出码 |
| ⑤ 会话与记忆 | 会话落 `/data/nexus/agent/sessions/`，跨会话记忆落 `/data/nexus/agent/memory/` |

**适配器 SPI**：
```python
class AgentAdapter:
    name: str
    def available(self) -> bool          # 探测是否可用
    def run(self, task, *, sandbox, policy, mcp_config, audit) -> AgentResult
```
- `local`：Ollama 驱动的最小工具调用循环（离线唯一可跑路径）
- `cli`：包装 `claude` / `opencode`（有网时接管）
- `mock`：确定性回放（CI 与事件总线测试）

**沙箱启动**（`systemd-run --user`）：
```
--property=ProtectSystem=strict
--property=ProtectHome=read-only
--property=PrivateTmp=yes
--property=NoNewPrivileges=yes
--property=ReadWritePaths=/data/nexus
--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
--property=SystemCallFilter=@system-service
--property=MemoryMax=<policy 中配置>
```
`--unsafe` 时仅去掉 `ProtectSystem=strict` 与 `ReadWritePaths` 限制。

### 3.5 事件总线（nexus-event-router）★ 差异化核心

**这是「系统被 agent 驱动」的落点，也是 Omarchy 唯一被公认做出 OS 深度的那层。**

```
journald (journalctl -f -o json)
   │
   ▼
规则匹配器 (events.toml)
   │  match: SYSLOG_IDENTIFIER / MESSAGE_ID / PRIORITY / 正则
   ▼
诊断上下文构建器 (nexus-doctor)
   │  并发采集: systemctl show / nvidia-smi / df / free / dmesg tail / coredump 字段
   ▼
agent 派发 (适配器: local 离线可用 / cli 有网)
   │
   ▼
处置建议 + 审计 (自动执行 / 仅建议 两档，由 policy 决定)
   │
   ▼
归档 /data/nexus/incidents/<date>/<id>.json + .md
```

**内置规则**：

| 规则 | 触发 | 动作 |
|---|---|---|
| `crash` | `CODE_DUMPED` / `status=11/6/7` | 崩溃自诊断：拉 coredump 元数据 + 相关日志 → agent 给出根因与修复步骤 |
| `gpu_oom` | `nexus.gpu.oom` 或 ollama 报 OOM | 自动卸载驻留模型 + 建议降级模型 |
| `disk_full` | `ENOSPC` / 阈值 | 列出大目录 + 建议清理（不自动删） |
| `unit_failed` | `systemd` 单元 failed | `systemctl show` 全量属性 + 最近日志 → agent 诊断 |
| `kernel_oops` | `Oops` / `BUG:` | 收集内核日志 → agent 分析 |

**关键点**：事件总线**常驻**（systemd --user 长跑单元），但**不常驻模型**——
事件到达时按需拉起 Ollama，闲置后由 broker 卸载。这解决了「事件驱动」与「显存预算」的矛盾。

### 3.6 对话式控制面（nexus CLI）

```
nexus "把 dolphin3 卸了，换成 14B"    # 自然语言 → agent → 工具调用 → 真实执行
nexus status                            # 系统 + AI 栈总览
nexus doctor [--since 1h]              # 主动诊断
nexus models list|load|unload|route    # 模型与显存
nexus gpu                              # 显存快照 + 占用归因
nexus events list|test <rule>          # 事件规则
nexus agent run --adapter local "..."  # 直接调用 agent
nexus mcp list|test                    # MCP server 自检
nexus logs [audit|events|gateway]      # 日志
nexus reset [--snapshot]               # 回厂
```

`nexus "<自然语言>"` 的执行链：
```
自然语言 → local/cli adapter → agent 决策 → MCP 工具调用（nexus-* 命名空间）
        → 沙箱内执行 → 审计落盘 → 返回结果 + 变更摘要
```

### 3.7 MCP server（nexus-mcp，stdlib 实现）

MCP = JSON-RPC 2.0 over stdio。标准方法：`initialize` / `notifications/initialized` / `tools/list` / `tools/call`。

**工具命名空间**（单进程多工具组，`nexus-mcp --server all`）：

| 命名空间 | 工具 |
|---|---|
| `fs.*` | `read` `write` `list` `stat` `mkdir`（路径受沙箱约束） |
| `shell.*` | `run` `which`（命令受 policy + 沙箱约束） |
| `git.*` | `status` `log` `diff` `branch` |
| `sysd.*` | `list` `show` `status` `restart` `logs` |
| `gpu.*` | `snapshot` `load` `unload` `models` |
| `journal.*` | `query` `tail` `errors` |
| `nexus.*` | `registry` `broker` `incident` `audit` |

---

## 4. 目录与配置规范（两级合并）

```
$XDG_CONFIG_HOME/nexus/          # 用户层（可写，覆盖系统层）
├── nexus.toml                   # 全局配置
├── models.toml                  # 模型注册表
├── events.toml                  # 事件规则
├── policy.toml                  # 权限策略
├── prompts/                     # system prompt / skill 注入
└── mcp.json                     # MCP client 配置（生成）

/etc/nexus/                      # 系统层（存在才读，只读默认）

/opt/nexus/
├── base/                        # 只读基底
└── live -> /data/nexus/live     # 原子可换符号链接

/data/nexus/                     # 持久层（rootless profile 下用户可写）
├── live/                        # 可变层实体
├── models/                      # 模型（默认软链到 /mnt/e/Ollama/models）
├── agent/{sessions,memory}/
├── incidents/
└── log/{audit,events,gateway}/

/run/user/<uid>/nexus/           # 运行时（pid、socket）
```

**配置合并规则**：`系统层 → 用户层` 逐键覆盖；列表按 `name` 合并而非替换。

---

## 5. systemd --user 单元清单

| 单元 | 类型 | 作用 |
|---|---|---|
| `nexus-gateway.service` | 常驻 | AI 网关（OpenAI 兼容门面） |
| `nexus-broker.service` | 常驻 | 显存仲裁器（后台守护 + 按需卸载） |
| `nexus-event-router.service` | 常驻 | 事件总线（journald 订阅） |
| `nexus-ollama.service` | 按需 | Nexus 专用 Ollama 实例（11435，共享 Windows store） |
| `nexus-agent@.service` | 模板 | agent 任务（systemd-run --user 沙箱实例） |
| `nexus-healthcheck.timer` | 定时 | 后端探活 + 显存快照归档 |

启动序：`nexus-ollama`（按需）→ `nexus-gateway` → `nexus-broker` → `nexus-event-router`

---

## 6. 安全模型（信任边界）

```
┌─ 用户主动调用 ────────────────────────────────────┐
│  nexus "..."  →  agent → MCP 工具 → 沙箱内执行     │
│                   ▲                                │
│                   │ 全部经过 systemd 沙箱          │
├─ 事件自动触发 ────────────────────────────────────┤
│  journald 事件 → agent → 处置                     │
│   policy: 自动执行档（仅限只读+安全写）/ 仅建议档  │
└──────────────────────────────────────────────────┘

默认边界：
  ✓ 系统目录只读（ProtectSystem=strict）
  ✓ $HOME 只读，仅 /data/nexus 可写
  ✓ PrivateTmp
  ✓ NoNewPrivileges
  ✓ 内存上限（MemoryMax）
  ✓ 危险命令不阻断，但在沙箱内**无 root 能力**
  ✓ 全程审计 JSONL
放开方式（显式、可审计）：nexus --unsafe / policy.toml 提权
```

**这解决了评审 H4**：v0.1 的「软约束不阻断」等于 Omarchy 4.0 的 docker 组漏洞形态。
v0.2 默认边界是**内核级**的，不是提示词级的。

---

## 7. Phase 计划与验收标准

### Phase 0 · 地基（bootstrap + 分层 + 配置）
**交付**：`nexus-bootstrap`（幂等安装器）、分层管理器、配置合并、目录规范、user 单元安装
**验收**：
```
nexus status          # 打印分层状态 + 配置来源 + 单元状态
nexus reset --dry-run # 打印将重建的路径
systemctl --user list-unit-files 'nexus-*'   # 单元已注册
```

### Phase 1 · 资源仲裁（registry + broker + gateway）
**交付**：模型注册表、显存仲裁器、OpenAI 兼容网关、路由决策
**验收**：
```
nexus models list                 # 4 个模型 + 体积 + fit 判定
nexus gpu                         # 实际占用 + 归因
curl localhost:8080/v1/models     # 合并模型列表
nexus models load qwen3.6:27b     # 必须被拒绝并给出降级建议（预算不足）
nexus models load dolphin3:8b    # 必须成功
curl localhost:8080/v1/chat/completions -d '{"model":"dolphin3:8b","messages":[...]}'
```

### Phase 2 · Agent 宿主（沙箱 + MCP + 审计）
**交付**：适配器 SPI、systemd 沙箱启动器、MCP server（stdlib）、审计落盘
**验收**：
```
nexus mcp test                    # initialize/tools/list/tools/call 往返
nexus agent run --adapter local "列出所有模型"   # agent 真的调了 MCP 工具
nexus agent run --adapter local "写一个文件到 /data/nexus/live/x"  # 沙箱内可写
nexus agent run --adapter local "写一个文件到 /etc/x"  # 必须失败（只读）
cat ~/.config/nexus/log/audit/*.jsonl | tail -1   # 审计有记录
```

### Phase 3 · 事件总线（系统反向驱动 agent）
**交付**：journald 订阅、规则引擎、诊断上下文构建、事件 → agent 派发
**验收**：
```
nexus events test crash           # 人工构造崩溃，触发规则，agent 产出诊断
nexus events list                 # 规则表 + 最近命中
ls /data/nexus/incidents/         # 产出诊断归档
```

### Phase 4 · 对话式控制面收口 + 裸机 profile
**交付**：`nexus "<自然语言>"` 全链路、overlayfs 后端、build 脚本、ISO 产出

---

## 8. 已知限制（诚实登记）

| 限制 | 影响 | 后续 |
|---|---|---|
| 无外网 | `cli` 适配器不可用，只能 `local` | 代理恢复后自动探测切换 |
| 无 sudo | 不能装包、不能建 system unit、不能 kernel overlayfs | 裸机 profile 用 root |
| 无 `systemd-coredump` 工具 | 崩溃元数据靠 journald 字段而非 coredumpctl | 裸机装 `systemd-coredump` |
| drvfs 上模型 mmap 慢 | 首次加载耗时可能显著高于原生 ext4 | 模型可选择性复制到 ext4 |
| Windows 侧占 8.3GB 显存 | 27B 只能 CPU offload | 空闲时释放 ComfyUI 即可全速 |
| 无 `jq` | JSON 处理全用 Python | — |

---

## 9. 与 v0.1 的关系

v0.1 的**分区思路**（§2.3）、**保留 systemd 的判断**（§1.2）、**取舍的诚实度**（§1.2）被完整继承。
v0.1 的**双后端共存**、**自研 harness**、**软约束权限**、**overlay+tmpfs** 被推翻，
理由逐条记录在 §0.3，证据在 `00-DESIGN-REVIEW.md`。
