# NexusOS 试玩手册

> 全部命令在 **WSL2 的 Ubuntu 里**执行（Windows 侧按 `wsl -d Ubuntu` 进入）。
> NexusOS 已装在 `~/.local/share/nexus`，命令在 `~/.local/bin`（已在 `.bashrc` 配好 PATH）。
>
> 建议按顺序跑，每一步都有"预期看到什么"和"这在证明什么"。

---

## 0. 进入环境

```bash
# Windows 侧（PowerShell / CMD）
wsl -d Ubuntu
```

进去之后所有命令直接可用。

**第一次进来**：直接敲 `nexus`（不带参数）就会走一遍新手引导 ——
6 步，每一步都**真的执行命令并展示真实输出**，约 30 秒。
想重看：`nexus tutorial`；想清进度重来：`nexus tutorial --reset`。

```bash
nexus status
```

---

## 1. 五分钟版（先跑这四条）

### 实验 1 · 全景

```bash
nexus status
```

**预期**：一张框起来的表 —— 分层后端、数据/配置目录、GPU 占用与显存预算、驻留模型、
后端健康（ollama 是 / vllm 否）、MCP 工具集、4 个 nexus 单元全部 `active`。

**证明**：这不是一个跑在 WSL 里的脚本，而是一套**常驻的系统层**（4 个 systemd --user 单元在跑）。

### 实验 2 · 真实显存

```bash
nexus gpu
```

**预期**：显卡型号、已用/总量/空闲、利用率、温度、功耗、显存预算，
下面一张模型表，每行带 `适配` 档位（gpu / partial / cpu）和判定依据。

**证明**：这些数字是**现采的**。可以同时开一个 Windows 终端跑 `nvidia-smi` 对照。

### 实验 3 · 模型注册表

```bash
nexus models list
```

**预期**：4 个模型（dolphin3:8b / dolphincoder:15b / qwen3.6:27b / qwen3-14b-abliterated），
带显存占用、参数量、量化、适配档位。

**证明**：注册表是**唯一真相源**——它同时知道"后端现在挂着谁"和"共享 store 里有哪些模型可选"。

### 实验 4 · 对话式控制面 ⭐

```bash
nexus "现在显卡占了多少显存？有哪些模型能用？"
```

**预期**：25 秒左右，先打印"→ 交给 agent（适配器 local，沙箱 strict）…"，
然后是中文结论（真实数字），最后列出**实际发生的工具调用**：

```
── 工具调用 (1) ──
  gpu.snapshot({}) → ok
```

**证明**：自然语言 → 本地 LLM 决策 → MCP 工具调用 → 真实系统事实 → 结论。
数字不是模型编的，是工具取回来的。

> 想看更快：用 `nexus agent run --adapter mock "检查系统"`（确定性，不调模型，秒级）。

---

## 2. 核心实验

### 实验 5 · 显存仲裁会拒绝你 ⭐

```bash
nexus models load qwen3.6:27b
```

**预期**：退出码 1，`"ok": false`，原因写明"显存严重不足"，
并给出**可执行建议**——这里会提示它虽然在共享 store 里，但需要先 stage 到本地：

```
"advice": ["先 stage 到本地 ext4 仓库再加载（drvfs 上加载极慢）：nexus models stage qwen3.6:27b"]
```

> **为什么这个拒绝是确定的**（不是看运气）：16GB 卡的总显存 16384MB，扣掉 1024MB 预留后
> 预算是 15360MB。27B 需要 16057MB > 15360MB，**无论 Windows 侧此刻占不占显存，
> 它都不可能完全驻留**。判定看的是"预算"而不是"当前空闲"——这是刻意的，
> 目的是守住给系统和其他进程留的余量。

再试装得下的：

```bash
nexus models load dolphin3:8b
```

**预期**：成功。若之前有别的模型驻留，会显示它**驱逐了谁**（独占语义）。

**证明**：单卡上"双后端共存"必然显存翻倍 → OOM。NexusOS 用**注册表 + 独占加载 + 预算**从根上避免。

### 实验 6 · 系统事件自动驱动 agent ⭐⭐（差异化核心）

```bash
nexus events test crash
```

**预期**：先打印"合成事件命中规则 crash，开始构建事实快照并派发 agent…"，
然后是 agent 的中文诊断（基于真实的主机负载/显存/错误日志），最后给出归档路径。

看归档：

```bash
ls -t ~/.local/share/nexus/incidents/*/ | head
cat "$(ls -t ~/.local/share/nexus/incidents/*/*.md | head -1)"
```

**证明**：这是 Omarchy 唯一被公认做出 OS 深度的那一层（coredump → agent 自诊断），
在 NexusOS 里被一般化成**可配置的规则引擎**。规则表：

```bash
nexus events list
```

内置 5 条：crash / gpu_oom / disk_full / unit_failed / kernel_oops。

### 实验 7 · MCP 协议与策略边界

```bash
nexus mcp test
```

**预期**：四段输出 —— `initialize` 握手 → `tools/list`（26 个工具）→
`tools/call gpu.snapshot`（真实快照）→ **策略边界测试：写 /etc 被拒**。

看全部 26 个工具：

```bash
nexus mcp list
```

**证明**：agent 能用的系统能力是**有 schema、带约束**的，不是"随便开个 shell"。

### 实验 8 · 信任边界全貌

```bash
nexus explain
```

**预期**：完整的 systemd 沙箱属性清单（ProtectSystem=strict / ProtectHome=read-only /
ReadWritePaths=… / NoNewPrivileges / PrivateTmp / 内存上限…），
**以及 agent 实际收到的 system prompt 全文**。

**证明**：边界是**内核级**的，不是提示词里写"请不要删文件"。

### 实验 8b · 权限档位 ⭐（0.3.0 新增）

```bash
nexus permissions          # 当前档位 + 可写根 + 沙箱属性
nexus permissions list     # 四个档位一览
```

**预期**：显示当前是 `standard`（标准），可写根含 `/home/andy`，
沙箱属性里有 `ReadWritePaths=/home/andy`。

**证明**：权限不是写死的，是**可分档调节**的。一个档位同时驱动沙箱层和工具策略层，
两层不会再各说各话。

验证档位真的生效（同一请求，两档两结果）：

```bash
# 默认档：能写自己的家目录
nexus "在 ~/nexus-probe.txt 写一行 hello"     # → 成功

# 临时收紧到 strict：内核层直接拦
NEXUS_TIER=strict nexus "在 ~/nexus-probe.txt 写一行 hello"
# → Errno 30 Read-only file system（内核只读挂载，不是软件层的"策略拒绝"）
```

`strict` 下报的是 `OSError` 而不是"策略拒绝"——这个差别就是双层边界的证据。

---

## 3. 破坏性实验（最有说服力）

### 实验 9 · 让 agent 试图改系统目录

```bash
nexus "在 /etc/nexus-hacked 写一个文件，内容是 hello"
```

**预期**：agent 会尝试调用 `fs.write`，然后**被拒绝**：

```
策略拒绝写入 /etc/nexus-hacked（可写根: ['~/.local/share/nexus', '~/.config/nexus']）
```

**证明**：双层边界。MCP 策略层先拦（工具级），
即便绕过它，systemd 沙箱层还有 `ProtectSystem=strict` 兜底 —— 实测报 `Read-only file system`。

### 实验 10 · 所有动作都留痕

```bash
nexus audit
```

**预期**：按类型统计的审计表（`mcp.call` / `agent.run` / `agent.result` / `event.match` / …）。

看原始记录：

```bash
tail -3 ~/.local/share/nexus/log/audit/$(date +%Y-%m-%d).jsonl | python3 -m json.tool --json-lines
```

**证明**：即使你显式放开沙箱（`--tier off`），动作依然留痕（记为 `agent.run.unsafe`）。

### 实验 11 · 一键回厂

```bash
nexus reset --dry-run    # 先看会发生什么
nexus reset              # 真回厂
```

**预期**：可变层被重建为基底状态，原子换链接。

**证明**：分层管理的价值 —— 你的改动落在可变层，基底永远干净。

---

## 4. 发布与更新

```bash
nexus version              # 版本 + 待执行迁移
nexus channel list         # 四条通道
nexus channel set edge     # 切通道
nexus channel set stable   # 切回来
nexus update --dry-run     # 看更新会做什么（不改任何东西）
```

> **注意**：`nexus update --dry-run` 会真的去**取一次产物**（只是为了 dry-run 也不联网），
> 所以在没有外网的机器上这一步会失败并退出 1——这是**如实反映**而非 bug。
> 只想看本地流程（空间检查 → 快照 → 迁移）用：
>
> ```bash
> nexus update --dry-run --skip-fetch
> ```

**`nexus update` 的固定顺序**（和 Omarchy 对齐）：
空闲空间检查 → 快照（回滚点）→ 取产物 → **迁移** → 换基底 → 重启单元。

**为什么只有它能更新**：直接 `apt upgrade` 会跳过快照与迁移 —— 这是有意的。

---

## 5. 自检套件

```bash
cd /mnt/e/LongevityLLM/nexusOS
bash tests/selftest.sh
```

**预期**：20 项全绿，末尾 `结果: 20 通过 / 0 失败`。

---

## 6. 服务管理

```bash
systemctl --user status nexus-gateway          # 网关
systemctl --user status nexus-event-router     # 事件总线
systemctl --user status nexus-broker           # 显存仲裁
systemctl --user status nexus-ollama           # 本地推理后端
systemctl --user restart nexus-gateway         # 重启单个
journalctl --user -u nexus-event-router -f      # 实时看事件总线日志
```

网关是 OpenAI 兼容的，任何 OpenAI SDK 都能接：

```bash
curl -s http://127.0.0.1:8080/v1/models | head -c 300
curl -s http://127.0.0.1:8080/healthz | head -c 200
```

---

## 7. 两件先说清楚的事

### 7.1 这东西会动你的系统吗？

**会，而且这是刻意的。** 默认档位是 `standard`：agent 能改你的家目录
（包括项目代码），能执行 shell 命令，能联网。**系统目录（`/etc`、`/usr`）仍然只读**。

| 方面 | 实际情况 |
|---|---|
| 需要 root 吗 | **不需要**。全程 `systemd --user` + 沙箱，不碰系统单元 |
| 改 `/etc`、`/usr` 吗 | **不会**。沙箱 `ProtectSystem=strict`，系统目录只读挂载 |
| 改你的家目录？ | **会**（默认 `standard` 档）。这是它能干活的前提 |
| 装系统包？ | 不会。无 root、无外网、也不碰 apt |
| 开机自启？ | 会（`systemd --user` + linger，你已开过 linger）。不想自启就 `systemctl --user disable nexus-*` |
| 危险命令？ | `rm -rf /`、`mkfs`、`dd` 写裸盘等**所有档位都拦** |
| 网络 | 允许 `AF_INET`（本地 Ollama 需要）。云端 API 需要外网 |
| 想更严 | `nexus permissions set strict` —— 只剩 nexus 数据区可写 |

**当前档位随时可查可改：**

```bash
nexus permissions            # 看当前档位与可写根
nexus permissions list       # 四个档位一览
nexus permissions set strict # 收紧（共享机器建议用这个）
nexus permissions set open   # 放开（/etc 等也可写，谨慎）
```

完整模型见 `docs/05-PERMISSIONS.md`。

**唯一真正的资源消耗是 GPU 显存**：本地 agent 跑一次会把 `dolphin3:8b`（约 5GB）驻留进显存。
如果你正在用 ComfyUI 跑图，这是**直接冲突**（WSL 与 Windows 共享同一块显存）。跑完手动释放：

```bash
nexus models unload dolphin3:8b
# 或者干脆让 broker 兜底：它发现超预算会自动卸载
```

### 7.2 首次运行会慢

第一次调用本地 agent 时，模型要加载进显存。实测：
从本地 ext4 仓库加载约 **44 秒**；如果权重在 Windows 盘（drvfs）上则要 **125 秒**。
NexusOS 自己的 ollama 实例指向 ext4 仓库，所以是 44 秒。**之后就常驻**，后续调用秒级。

---

## 8. 卸载

```bash
systemctl --user disable --now nexus-gateway nexus-broker nexus-event-router nexus-ollama
rm -rf ~/.local/share/nexus ~/.config/nexus ~/.local/bin/nexus ~/.local/bin/nexus-mcp
```

---

## 9. 建议的试玩顺序（5 分钟内看到全部核心）

```bash
nexus status                                    # 1. 全景
nexus gpu                                       # 2. 真实数据
nexus models load qwen3.6:27b                   # 3. 看它拒绝你 + 给建议
nexus "现在显卡占了多少显存？"                    # 4. 对话式（最有戏）
nexus events test crash                         # 5. 事件驱动 agent
nexus "在 /etc/nexus-hacked 写一个文件"           # 6. 看它被拒绝
nexus audit                                     # 7. 看所有留痕
```

**第 4 步和第 6 步是这个系统真正区别于"装了 Ollama 的 Ubuntu"的地方。**
