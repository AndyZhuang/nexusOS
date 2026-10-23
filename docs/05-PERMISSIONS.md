# NexusOS 权限模型

> 版本：0.3.0 ｜ 唯一真相源：`src/nexus/permissions.py`

## 1. 为什么要重做

v0.2 的权限设计有一个**结构性问题**：两层各管各的，互不通气。

| 层 | v0.2 的实现 | 实际问题 |
|---|---|---|
| systemd 沙箱 | `strict` / `standard` / `off` 三档 | 只对 `cli` 适配器生效 |
| MCP 策略层 | `write_roots` 写死两个 nexus 目录 | **与档位完全脱钩** |

后果有两个：

1. **"双层边界"是假的**。`local` 适配器的工具调用在 `nexus` 主进程内直接执行，
   `sandbox_tier` 参数只被写进审计日志。真正拦得住的只有 MCP 策略层。
   实测证据：v0.2 下 agent 写 `/etc` 被"策略拒绝"——那是**软件层**的检查，
   不是内核层的只读挂载。

2. **严格程度和用户想做的事对不上**。`write_roots` 只有 nexus 数据区，
   agent 连你自己的项目目录都改不了。一个不能改代码的 agent 没有实用价值。

## 2. 现在的模型：一个档位驱动两层

```
                    nexus permissions set open
                              │
                              ▼
                         tier（档位）
                        ╱          ╲
                       ╱            ╲
              systemd 沙箱属性    MCP 工具策略
              （内核级强制）      （软件层拦截）
              ProtectSystem      write_roots
              ProtectHome        read_roots
              ReadWritePaths     denied_commands
```

一个 tier 同时决定两层参数，**不再可能各说各话**。

### 2.1 四个档位

| 档位 | 可写根 | systemd 关键属性 | 适用场景 |
|---|---|---|---|
| `strict` | nexus 数据区、配置区 | `ProtectSystem=strict` + `ProtectHome=read-only` | 共享机器、多租户 |
| `standard` | **家目录**、nexus 数据区、配置区 | 同上 + `ReadWritePaths=$HOME` | 单人工作站（**默认**） |
| `open` | 家目录、`/etc`、`/opt`、`/srv`、`/usr/local` | 同上 + `ProtectHome=no` | 把整台机器托付给 agent |
| `off` | `/` | 不套任何沙箱 | 调试、完全信任 |

所有档位共有：`NoNewPrivileges`、`PrivateTmp`、`ProtectKernelTunables`、
`ProtectKernelModules`、`ProtectControlGroups`、`RestrictSUIDSGID`、`LockPersonality`。

### 2.2 为什么不用 `PrivateDevices=yes`

本机是 GPU 工作站，`nvidia-smi` 依赖 `/dev/dxg`。`PrivateDevices=yes` 会把设备节点
藏起来 → 沙箱内 GPU 查询直接失效 → 仲裁器拿不到显存状态 → agent 瞎一半。

所以这一项在所有档位都关闭，代价由其余内核级防护承担。这是明确的取舍，不是遗漏。

## 3. 运行时：agent 真的在沙箱里

v0.2 的问题不是"沙箱不够严"，而是**沙箱没套到 agent 身上**。0.3.0 改成：

```
nexus "任务"
   └─ 父进程：不进沙箱，负责派生 + 收结果
        └─ systemd-run --user -p <档位属性> python3 -m nexus.cli __agent-child
             └─ 子进程：在内核沙箱里跑真正的 agent 循环
                  └─ MCP 工具调用 → 在这个沙箱里执行
```

子进程通过环境变量 `NEXUS_SANDBOX_CHILD=1` 标记自己，避免套两层。
`cli` 适配器在子进程内不再二次套沙箱（外层已经按目标档位起好了）。

### 实测证据（2026-10-23）

同一个请求（写 `~/nexus-agent-probe.txt`）在两个档位下的结果：

| 档位 | 结果 | 拦在哪一层 |
|---|---|---|
| `standard` | ✅ 文件真的生成了 | — |
| `strict` | ❌ `OSError: [Errno 30] Read-only file system` | **内核层**（systemd 只读挂载） |

`strict` 下的报错是 `OSError` 而不是"策略拒绝"——这个差别很关键：
前者来自内核的只读挂载，**绕过 MCP 策略层也拦得住**。
这才是双层边界该有的样子。

审计日志里每次 agent 运行会出现**两条** `agent.run` 记录（父 + 子），
可据此确认派生确实发生了。

## 4. 底线：危险命令黑名单

以下命令在**所有档位**（含 `off`）都被拦：

```
rm -rf /        fork 炸弹        mkfs        dd if=... of=/dev/...
shutdown        reboot           > /dev/sdX  wipefs
```

除非在 `policy.toml` 里显式写 `denied_commands = []`。

> 注意：黑名单是**正则黑名单**，不是语义分析器。绕过写法（变量拼接、
> `find -delete` 等）不在覆盖范围内 —— 它是防手滑，不是防攻击者。

## 5. 怎么用

```bash
nexus permissions                 # 看当前档位与边界
nexus permissions list            # 列出全部四个档位
nexus permissions set open        # 切档（写进 policy.toml 的 default_tier）
nexus permissions set strict      # 切回严格档
```

优先级：**命令行 `--tier` > 环境变量 `NEXUS_TIER` > `policy.toml` 的 `default_tier` > 默认值**。

```bash
NEXUS_TIER=strict nexus "某个任务"    # 单次调用临时收紧
```

### 路径根从哪来

档位推导路径根，**不写死在 `policy.toml`** —— 写死会架空档位
（显式值优先于档位默认值）。`nexus permissions set` 会主动清除已写死的
`read_roots` / `write_roots` 并明确告知。

真想手动加路径，就在 `policy.toml` 里写 `write_roots = [...]`，
那会覆盖档位默认（此时档位只影响沙箱层）。

## 6. 诚实说明风险

`standard` 及以上档位下，agent：

- **可写你的家目录**（含 `~/.ssh`、`~/.config`、项目代码）
- **可发起网络连接**（`AF_INET` 必须开，本地推理后端走 HTTP）
- **可执行 shell 命令**（受黑名单约束）

所以：

- 这是**给 agent 干活用的**，不是防恶意代码的隔离边界。
- 共享机器 / 多租户请用 `strict`。
- 跑在别人机器上、或让 agent 处理来路不明的输入时，**别用 `open`**。
- 所有动作都留审计（`nexus audit`），包括显式放开沙箱的 `agent.run.unsafe`。

## 7. 相关文件

| 文件 | 作用 |
|---|---|
| `src/nexus/permissions.py` | 档位定义、路径根推导、沙箱属性、策略合成 |
| `src/nexus/sandbox.py` | systemd-run 执行器（薄封装，属性来自 permissions） |
| `src/nexus/mcp.py` | `Policy` 类，消费 permissions 的合成结果 |
| `src/nexus/agent.py` | 沙箱子进程派生与结果回收 |
| `migrations/1753500000.sh` | 既有安装从"写死路径"迁移到"档位驱动" |
