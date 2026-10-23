# NexusOS-Developer 设计评审

> 评审对象：`NexusOS-Design-Developer.md（个人开发者版）.md` v0.1
> 评审日期：2026-10-23
> 评审结论：**方向对，但骨架错。** 这不是一份需要修补的设计，是一份需要重写主线的设计。

---

## 0. 一句话结论

这份文档写的是「一台装了 Ollama 和 vLLM 的 Ubuntu」，标题却叫「AI-Native 操作系统」。
方向反了——Omarchy 的赌注不是「AI 作为系统服务」，而是「agent 与系统共享上下文、agent 能反过来改系统」。
按现在这份文档做下去，你会得到一个**更漂亮的 Ubuntu Minimal + 一堆 CLI 包装**，
而 Omarchy 那句 *"Every agent, pre-wired, lazy-loaded"* 的灵魂一点没继承到。

---

## 1. 做对了什么

1. **分区/数据分离的思路是对的。** `/data` 独立持久、`models`/`agent`/`log` 分区，是任何长期跑本地模型的机器的正确做法。文档第 2.3 节的结构可以直接继承。
2. **保留 systemd 是清醒的。** Developer Edition 砍掉 systemd 会直接失去 cgroup、namespace、socket activation、coredump、journald 这五件套——而这五件套恰好是 AI-native 系统的地基（见 §5）。
3. **取舍写得很诚实。** 明确列出「❌ 不做 TPM 密封、不做内核裁剪」比含糊其辞强得多。问题只在于「Developer 版=软约束」这一步迈得太远（见 H4）。

---

## 2. 硬伤（按严重度排序）

### H1 · overlayfs 根 + tmpfs 上层 = 要么 OOM，要么丢改动

文档第 2.3 节：`/` = overlayfs，底层 ro，**上层 tmpfs**。第 69 行又说「重置是可选开关，默认可关闭」。

这两句互相矛盾。overlayfs 的 upperdir 必须是一个真实文件系统：
- upper 在 tmpfs → 所有写入（apt install、pip install、编译产物、Docker 镜像层 `/var/lib/docker`）全进内存。Developer Edition 保留 Docker + 编译器 + venv，装一个 `torch` 就吃掉几个 GB。**这不是精简，这是给内存挖坑。**
- upper 落盘 → 根目录就退化成普通可变系统，overlayfs 白加；而「重启清零」这个卖点也没了。

**修法：** upperdir 固定落在 `/data/.overlay/upper`（ext4），overlayfs 只负责「一键回出厂」这一个功能；
想要 tmpfs 语义时，用 `nexus-reset` 显式清空 upper，而不是让 tmpfs 常驻。

### H2 · 「双后端共存」在单卡上直接死

文档第 96 行：「简单路由：小请求走 Ollama，批量走 vLLM」。

没有定义什么叫「小」。真实执行路径是：同一个模型在 Ollama 和 vLLM 各加载一份权重 → **显存翻倍**。
本机实测（见 §6）：RTX 3080 Laptop 16GB，**已有 8.3GB 被 Windows 侧进程占用、GPU util 100%**，只剩约 8GB。
一个 7B Q4 权重 ≈ 4.5GB，Ollama + vLLM 各一份 ≈ 9GB → 必然 OOM。

**修法：** 引入**模型目录（model registry）作为唯一真相源**，模型 → 后端 → 显存占用 → 加载状态；
**同一模型在任一时刻只允许一个后端持有权重**，切换 = 卸载 + 加载。「双后端」变成「可切换的后端栈」，不是「共存的两个人」。
再加一条显存预算线（`gpu_budget_mb`），超限时网关拒绝加载并给出可执行的释放建议。

### H3 · 自研 Go/Rust harness = 时间黑洞，且与对标对象背道而驰

文档第 99 行：「自研轻量 harness（Go/Rust，静态编译）」。

Omarchy 的做法**恰恰相反**：它预装 10 个现成 agent CLI（Claude Code / OpenCode / Codex…），
用 **mise 懒加载**到 `~/.local/bin`，首次调用才下载，平时零占用。它的产品宣言是
*"Every agent, pre-wired, lazy-loaded"* —— **复用，不是自研**。

自研 harness 意味着你要自己解决：流式协议转换、tool-call 解析、上下文压缩与摘要、
权限模型、会话持久化、MCP 传输（stdio / SSE / streamable-HTTP 三种都要兼容）、
多 provider 的 tool-call 方言差异。这是几周的量级，而且没有社区兜底。

而且你的本机**已经有** `claude` 和 `opencode`（`D:\dev-cache\npm-global\`）。

**修法：** Nexus 不做 harness，做 **harness 的宿主**。职责收缩为五件：
① 系统级 system prompt / skill 注入　② 权限与 allowlist 策略　③ MCP server 供给
④ 审计与事件触发　⑤ 会话与记忆落盘到 `/data/agent`。agent 本体由 mise 式懒加载供给，可换。

### H4 · 「软约束不阻断」= Omarchy 4.0 同款漏洞，只是没被发现

文档第 101–103 行：「工具权限（Developer 版只做软约束）……高危命令仅做提示 + 日志记录，不强制阻断」，
同时第 23 行开放 user namespace + rootless 容器 + Docker。

**这正是 Omarchy 4.0 的安全事故复刻版。** Omarchy 4.0 的安装脚本默认把用户加进 `docker` 组，
HN 上 andrej 指出：**docker 组比 sudo 更危险，等价于 root**，且不警告用户。
4.0.1 才通过漏洞披露流程修掉（来源：https://blog.csdn.net/techforward/article/details/164288112）。

更危险的是叠加效应：**一个能执行 shell 的 LLM agent + 无强制阻断的权限模型 + prompt injection**
= 一次被投毒的网页/README 就能让 agent 拿到 root 等效权限。文档自己说"Developer 版够用即可"，
但这不是「够用」，这是「默认处于 Omarchy 修过的那个漏洞形态」。

**修法（不牺牲便利）：** 默认用 **bubblewrap 沙箱**包住 agent 进程：
只读挂载系统目录、`/data` 按需可写、网络默认关或白名单、危险命令仍不阻断但**被限制在沙箱内**。
开发者想放开时用 `nexus-run --unsafe` 显式提升。
成本很低（Ubuntu universe 里就有 bwrap），收益是**把 Omarchy 那个 CVE 从设计上消除**。

### H5 · 升级模型自相矛盾

第 208–209 行：推荐「重建 `/ro` 基准层原子替换」，同时允许「直接 `apt upgrade` 滚动更新」。

两套模型并存 = 系统状态不可预测。混用三个月后你会遇到「这个 bug 是 base 的还是 overlay 的」这类经典难题。

**修法：** Developer Edition 明确选 **mutable-first**：apt 是唯一真相源，`/ro` 只是出厂快照（供 `nexus-reset` 恢复）。
immutable（ostree）留给后续 Secure Edition 作为可选项。

### H6 · 分区方案与目录规范打架

- 第 2.3 节分区表里**没有 `/ro`**，但第 5 节目录规范里 `/ro` 是根目录下的一个顶层目录 —— 它到底是什么？
- `/data` 已经是独立分区，又在下面开 `/data/models`、`/data/agent`、`/data/log` 三个挂载点 —— 同盘上开子挂载点除了路径好看没有任何收益（除非做 quota/加密，目前没提）。
- 第 151–152 行把 `/var/lib/ollama` 软链到 `/data/models/ollama`，但 `/var/lib` 在 overlayfs 的 upper 里 → 每次重置这个软链就没了。

**修法：** 只留 `/`（overlay）、`/data`（含 models/agent/log 子目录，不用子挂载点）、`/boot` 三个挂载点；
`/ro` 明确为 overlayfs 的 lowerdir 实际路径（`/mnt/nexus/ro`）；所有软链由 `tmpfiles.d` 在启动时重建。

### H7 · vLLM 开机自启 = 每次重启都可能抢爆显存

第 168 行把 `vllm.service` 和 `ollama.service` 并列为开机服务。
vLLM 启动要 30–120 秒加载权重、常驻 60–90% 显存，且经常启动即 OOM 崩溃。
开机必启 = 每次重启都抢一次显存，然后可能把 Ollama 也拖死。

**修法：** vLLM 改为 **socket activation / 按需拉起**（首次请求时起，闲置 N 分钟后自动卸载），或显式 `nexus-bench` 模式才启动。

### H8 · 缺了「AI 原生」最核心的那条线：系统没有被 agent 驱动

通读全文，AI 在这份设计里的角色是**被调用的服务**（`ai "分析日志..."`），不是**系统的驱动者**。
全篇没有一处写「系统事件 → agent → agent 改系统」。

而这恰恰是 Omarchy 唯一被公认做出 OS 深度的那一层：
1. **崩溃自诊断** —— systemd-coredump 捕获 → 自动交给 agent 分析 → agent 给出修复方案；
2. **agent 用量面板** —— 每个 agent 的订阅状态、调用限额、按天按模型的 token 消耗；
3. **主题同步** —— agent 的 UI 跟随系统主题（说明 agent 被当成桌面环境的一部分）。

这三条在 NexusOS 文档里全部缺席。**这是最该补的部分，也是最便宜的部分**（全是 systemd 现成能力）。

### H9 · 裁剪清单不可执行

「移除……多余 manpage / locale」—— 没说怎么做。
实际上：locale 用 `localectl set-locale C.UTF-8` + `dpkg-reconfigure locales`；
manpage 要删 `manpages` / `manpages-dev` 包（会连带 `groff-base`，属正常）；
最彻底的做法是 `dpkg --path-exclude`。
但**debootstrap 出来的 Ubuntu rootfs 本来就不大**（约 300–500MB），
「Alpine 式干净精简 + Ubuntu 式 glibc 生态」这个目标本身是有张力的：真正 Alpine 干净靠 musl，
而 musl 会让 CUDA / PyTorch wheels 全线报废。**别在「精简」上过度投资，那不是 AI-native 的价值所在。**

### H10 · NVIDIA + 官方通用内核 + overlay 的组合没提 DKMS

文档第 46 行保留「官方通用内核（带完整模块，方便调试）」。但 NVIDIA 驱动与内核版本强绑定：
- 用预编译模块 → 每次内核升级都必须重装对应版本驱动；
- 用 DKMS → 需要 `linux-headers-$(uname -r)` + `build-essential` + `dkms`，而 minimal 镜像默认没有内核头文件。
官方推荐的是 `nvidia-open`（Turing+ 开源内核模块）或 `cuda-drivers`（专有模块）。
（来源：https://docs.nvda.net.cn/datacenter/tesla/driver-installation-guide/）

**修法：** 明确选定 `nvidia-open` 或 `cuda-drivers` 二选一，把 `linux-headers` + `dkms` 写进 base 包列表。

### H11 · 网络栈选择埋雷

第 72 行：「systemd-networkd（也可按需换 NetworkManager）」。

networkd 适合静态服务器网络，**没有 WiFi 支持**（wpa_supplicant 要自己接），
而个人工作站笔记本大概率要连 WiFi。NetworkManager 有完整 WiFi/热点/VPN 生态，
且它的 D-Bus 接口是 **结构化的**，对 agent 友好得多。

**修法：** Developer Edition 默认 NetworkManager（桌面/工作站语义），networkd 作为纯服务器 profile。

### H12 · Roadmap 没有一个可测指标

Phase 0~5 全是名词，没有验收条件。设计文档没有验收标准 = 没法开工。

---

## 3. 与 Omarchy 的真实落差

| 维度 | Omarchy（事实） | 本文档 | 落差 |
|---|---|---|---|
| 定位 | 桌面发行版（Arch + Hyprland + Quickshell） | 无头服务器发行版 | 「对标」基准就选错了轴 |
| Agent 来源 | 复用 10 个现成 CLI，mise 懒加载 | 自研 Go/Rust harness | 反着学 |
| OS 深度 | coredump→agent 自诊断、token 面板、主题同步 | 无 | 核心未继承 |
| 系统本体 | agent 可直接改 Hyprland 配置（Skills 机制） | agent 只读日志 | agent 无写权限 |
| 安全教训 | 4.0 加 docker 组 = root 等效，4.0.1 修复 | 「软约束，不阻断」 | 复刻了已修的漏洞 |
| 维护现实 | Omacom Foundation 募资 1260 万美元，有全职团队 | 个人自维护 | 自研 harness 必烂尾 |
| 规模 | 36.5k stars / 6308 commits | — | 需正视差距 |

**换句话说：Omarchy 值得学的是它的「操作系统哲学 + 系统级集成三件套」，不是它的包管理。**
你现在这份文档恰好学的是包管理层（裁剪、镜像、双后端），漏掉了哲学层。

---

## 4. 我建议的 v0.2 骨架（待你拍板的部分已标 ❓）

```
Layer 0  基座    Ubuntu 24.04 minimal / 可变系统 / overlayfs 根(upper 落盘)
                 NVIDIA nvidia-open + CUDA + nvidia-container-toolkit
                 NetworkManager（默认）/ systemd-networkd（server profile）
                 ← ❓基底/更新模型 待你选

Layer 1  资源   nexus-model-registry  模型目录（唯一真相源）
                 nexus-gpu-broker      显存预算 + 独占加载 + 自动卸载
                 nexus-ai-gateway      OpenAI 兼容统一门面，后端可切换（不共存）

Layer 2  代理   agent 供给：mise 式懒加载（claude / opencode / …）
                 nexus-harness-host    只做 5 件事：system prompt / 权限 / MCP / 审计 / 记忆
                 nexus-mcp-servers     文件 / shell / git / systemd / gpu / journald

Layer 3  事件   systemd OnFailure=       → agent 自动诊断（Omarchy 的 coredump 那一层）
                 journald 规则匹配        → 告警 agent
                 udev 事件（GPU 掉卡/USB）→ agent
                 GPU OOM watchdog        → 自动卸载 + 换后端建议

Layer 4  信任   bubblewrap 沙箱包裹 agent 进程（默认隔离）
                 nexus-run --unsafe      显式放开（不是默认）
                 审计日志全程落盘 /data/log/agent
```

**新增而原文档完全没有的三件：**
1. **事件驱动层（Layer 3）** —— 这是「AI-native」真正的落点，也是 Omarchy 唯一做对的部分；
2. **资源仲裁层（Layer 1）** —— 解决 H2 的显存双倍问题；
3. **信任边界（Layer 4）** —— 解决 H4 的 Omarchy 同款漏洞。

---

## 5. 落地环境实况（我已实测，可直接用）

> 结论先行：**这台 Windows 笔记本就是现成的试验田**，不需要额外硬件。

| 项 | 实测结果 |
|---|---|
| GPU | RTX 3080 Laptop 16GB，驱动 596.47 / CUDA 13.2 ✅ |
| WSL2 Ubuntu | **24.04.3 LTS**，`/etc/wsl.conf` 已开 `systemd=true`，PID1 = systemd ✅ |
| WSL GPU 直通 | `/dev/dxg` 存在，`/usr/lib/wsl/lib/libcuda.so.1` 就位，`nvidia-smi` 可用 ✅ |
| 显存占用 | **8361 / 16384 MiB 已用，util 100%**（Windows 侧进程占用，WSL 与 Windows 共享同一块显存）|
| Ollama | **0.15.2 已装**，`ollama.service` 已作为 systemd 单元运行中（当前 0 模型）✅ |
| systemd | 正常 running；journald ✅；tailscaled ✅；snapd ✅ |
| 工具链 | python3.12.3、git、gcc、make、curl、wget、systemctl ✅ |
| 缺失 | jq、bubblewrap、coredumpctl(未装 systemd-coredump)、docker、podman、vllm、pip、ensurepip |
| **网络** | **❌ WSL 与 Windows 均无外网**（代理 127.0.0.1:7897 关闭，github.com HTTPS 不通）→ **无法 apt / pip 安装任何东西** |
| Agent CLI（Win） | `claude`、`opencode`（`D:\dev-cache\npm-global\`）✅ |
| 磁盘 | C: 18G / D: 82G / E: 53G 空闲（三分区同属一块 NVMe）|

**这条实测直接改变了两件事：**
1. **构建必须零第三方依赖**（纯 stdlib / 或离线 vendor）。这恰好和同项目 `hidlab.py` 的风格一致，不是坏事。
2. **vLLM 在本机装不上**（需要 torch wheel，数百 MB 且无网）。所以「双后端」在本机只能是 Ollama 实跑 + vLLM 走 mock/远端接口。
3. **显存只有约 8GB 空闲**，再次印证 H2：共存方案不可行，仲裁层是刚需而非洁癖。

---

## 6. 需要你拍板的四件事

见本轮对话中的提问卡片。答完我就出 **v0.2 设计定稿 + Phase 0 可执行清单（含验收标准）**，
然后在 WSL2 里把 Layer 0/1/2 真实跑起来给你看。
