# NexusOS 发布模型 —— 从 Omarchy 学到什么

> 目标：把 NexusOS 从「我机器上的一个系统层」变成「任何人都能装、能升级、能回滚、能信任的发布物」。
> 参考对象：Omarchy（basecamp/omarchy + omacom-io/omarchy-iso + omacom-io/omarchy-pkgs）
> 日期：2026-10-23

---

## 0. 核心洞察

**发行版的本质不是 ISO，而是「有版本、有通道、能升级、能回滚、能签名」的分发体系。**
ISO 只是一次性引导手段——Omarchy 的 ISO 装了 5 分钟就完事，
真正让它成为一个"发行版"的是后面那套通道 + 迁移 + 快照 + 签名的组合。

这条洞察直接改变 NexusOS 的路线：
**不需要先做 ISO 就能发布。** 先把分发体系建起来，ISO 可以后补。

---

## 1. Omarchy 的发布工程：六个可复用机制

### 1.1 ISO 仓库与主仓库分离

Omarchy 有三个仓库，各司其职：

| 仓库 | 职责 |
|---|---|
| `basecamp/omarchy` | 系统本体（MIT）：`bin/ config/ default/ install/ migrations/ themes/ manual/` |
| `omacom-io/omarchy-iso` | ISO 构建与分发：archiso + Docker + QEMU 测试 + 签名 + 上传 |
| `omacom-io/omarchy-pkgs` | pacman 包仓库：Omarchy 本体以**包**的形式发布 |

**为什么重要**：ISO 构建（慢、易碎、需要 Docker）不会污染主仓库；
主仓库保持纯脚本与配置；包仓库负责可增量更新的分发。

> **NexusOS 映射**：`src/nexus/`（系统本体）与 `packaging/`（构建产物）已经是分离的，
> 保持这个边界。deb/tarball 走 `packaging/`，CI 走 `.github/workflows/`。

### 1.2 发布流水线分层（可组合）

`bin/omarchy-iso-make` / `-boot` / `-sign` / `-upload` / `-release`
—— 每个动作独立可单独调用，`release` 只是把它们串起来。

> **NexusOS 映射**：已实现 `bin/nexus-release`，同样分
> `build` / `test` / `sign` / `verify` / `upload` / `check` / `release`。
> 关键设计：**`test` 失败即拒绝发布**——`cmd_test` 里 `|| die "自检未通过，拒绝发布"`。

### 1.3 迁移机制（最值得学的一条）

Omarchy 的 `migrations/<Unix时间戳>.sh`：

- `omarchy-update` 用 `git diff --diff-filter=A $old..$new` 找出**新增**的迁移脚本，
  依次执行（`--diff-filter=A` 保证改过和删掉的不会重跑）；
- 完成标记落在 `~/.local/state/omarchy/migrations/`，**每用户只跑一次**；
- 迁移必须幂等；失败返回非 0，但**不中断后续包更新**（可用性优先于严格性）；
- 迁移可以 `source install/*.sh` 复用安装逻辑。

> **NexusOS 映射**：已实现 `src/nexus/update.py`。
> 差别与理由：Omarchy 的失败不中断是"滚动更新要保证包能装上"；
> NexusOS 的配置迁移失败会导致**后续迁移在错误的状态上跑**，所以我选择
> **失败即中止更新**（`break` + 返回错误），并把回滚点留在快照里。
> 这是有意识的分歧，不是照抄。

### 1.4 四条更新通道

| 通道 | 追踪什么 | 给谁 |
|---|---|---|
| `stable` | 官方发布 + **落后 Arch 官方一个月**的稳定镜像 | 所有人 |
| `rc` | 候选版，大版本前验证 | 帮忙打磨的人 |
| `edge` | 最新开发构建 + 最新上游包 | 能自己救系统的人 |
| `dev` | 直接跑 `~/omarchy` 的 git checkout | 在 Omarchy 上工作的人 |

`stable` 落后一个月是关键设计：**给配置变更留出缓冲期**，
让不兼容先在 edge 用户身上暴露，再决定 stable 要不要改配置。

> **NexusOS 映射**：已实现 `stable` / `rc` / `edge` / `dev` 四通道。
> 差别：Omarchy 的 stable 滞后靠"自建 Arch 镜像"实现；
> NexusOS 基底是 Ubuntu 24.04 LTS（本身是稳定发布），
> **滞后对象不是上游系统包，而是 NexusOS 自己的版本**——
> stable 只发正式 tag，edge 发持续构建。`dev` 通道直接用本地源码树。

### 1.5 快照与回滚

- Btrfs + Snapper + Limine 引导器（**不是** GRUB）
- 每次 `omarchy update` 自动打快照
- 快照恢复 **rootfs，不覆盖 `/home`**
- 引导菜单里选快照恢复，版本号标注在角落
- 快照失败只警告不中断（但会明确说"没有快照"）

> **NexusOS 映射**：没有内核级快照（rootless、无 Btrfs），
> 用**分层管理器的 live 快照**实现等价语义：
> `nexus update` 动手前先 `layer.snapshot("pre-update")`，
> 快照落在 `$DATA/snapshots/`，`nexus reset --snapshot` 可回滚。
> 语义边界一致：**恢复系统层，不动用户数据**。
> 差距要诚实登记：Omarchy 的快照是 rootfs 级、秒级、跨重启；
> NexusOS 的是数据目录级、秒级、跨重启，但只覆盖 NexusOS 自己的层。

### 1.6 拦截直接升级 + 安全基线

- pacman hook 检测 `pacman -Syu` / `yay -Syu` 并**中止事务**，指向 `omarchy update`
  （绕过需要 `OMARCHY_ALLOW_DIRECT_PACMAN=1`）
- ISO 与包仓库 PGP 签名，校验文件是 URL 追加 `.sig`
- LUKS 全盘加密默认开、防火墙默认只放行 LocalSend、ssh 默认关、AUR 默认不装

> **NexusOS 映射**：
> - 签名 → `bin/nexus-release sign` + 单文件安装器的 gpg 校验（`NEXUS_REQUIRE_SIGN=1` 可强制）
> - 拦截直接升级 → Ubuntu 上没有 pacman hook 那么干净的机制，
>   但有等价的 `DPkg::Pre-Invoke` apt hook（写在 `/etc/apt/apt.conf.d/`，需 root）。
>   **已列入下一阶段**，当前先用文档明确"只走 `nexus update`"。
> - 安全基线 → 已在 **Agent 沙箱层**做到内核级（`ProtectSystem=strict`），
>   这比 Omarchy 的 "passwordless sudo 临时提权" 路线更保守：
>   agent 拿不到 root，而不是"给你 30 分钟 root"。

### 1.7 附带的两个设计细节

- **CLI 是为 agent 设计的**：`omarchy` 的每个子命令都是小 helper
  （`omarchy-pkg-add` / `omarchy-notification-send`），
  可组合、可脚本化，官方明说"这套 CLI 主要为让 AI agent 帮你定制系统而设计"。
- **安装器的两种模式**：为他人安装（跳过个人配置，首启再问）/
  无人值守安装（配置放第二盘，定位 VM 与批量机器的 base image）。

> **NexusOS 映射**：`nexus` 已经是单一多级命令 + 全部子命令可脚本化。
> `mcp.py` 暴露 26 个工具，等于给 agent 提供了一套**结构化的系统操作 API**——
> 这比 Omarchy 的 shell helper 更进一步，因为工具调用是带 schema 的。

---

## 2. 哪些不学，以及为什么

| Omarchy 的做法 | 不学的理由 |
|---|---|
| 自建 Arch 镜像（落后一个月） | NexusOS 基底是 Ubuntu LTS，本身稳定；自建镜像需要持续同步成本，且 NexusOS 是单人项目 |
| 自研 `linux-omarchy` 内核 | 内核是安全边界最敏感的部分，Omarchy 有专职团队；NexusOS 复用上游内核 |
| Limine 引导器 + 快照菜单 | 引导器是 ISO 阶段的事；在有 root 的裸机上才需要 |
| 强制关闭 Secure Boot / TPM | NexusOS 是"层"，不接管引导链，不该对用户的机器做这种决定 |
| 桌面（Hyprland + Quickshell） | NexusOS 定位是**工作站/服务器侧**的系统层，桌面是另一条产品线 |
| 包仓库（pacman） | NexusOS 基底是 Ubuntu → 用 **deb + apt** 是生态原生路径，不是移植 pacman |

---

## 3. NexusOS 的发布形态：三阶段

### 阶段 A：层分发（**现在就能发布**）

产物与安装方式：

```
nexusos-<version>.tar.gz      # rootless profile，52KB
nexusos_<version>_amd64.deb   # 系统级 profile，48KB
```

| 路径 | 覆盖 | 需要 root |
|---|---|---|
| 单文件安装器 `curl ... \| bash` | 任意 Ubuntu 24.04 / WSL2 / 裸机 | 否 |
| deb 包 `apt install ./nexusos_0.2.0_amd64.deb` | 系统级 profile | 是 |
| tarball 手动解包 | 无 root 环境 | 否 |

分发渠道：GitHub Releases（tarball + sha256 + sig）、自建 deb 仓库、镜像站。
**当前阻塞**：本机无外网（代理关闭），`gh` 未登录 → 只能产出产物，不能上传。

### 阶段 B：仓库化（有人用之后）

- 自建 **deb 仓库**（`apt install nexusos` 能升级）
- 通道对应的仓库：`stable` 主仓 + `edge` 测试仓
- 稳定通道的"滞后"落在 **NexusOS 自己的版本**上：stable 只发正式 tag
- 签名密钥与 `.sig` 校验文件入库
- 自动更新：`nexus update` 拉 deb/tarball 并跑迁移

### 阶段 C：真 ISO（需要裸机 + root + 外网）

复用 Omarchy 的 `omarchy-iso` 结构：
`bin/nexus-iso-make`（archiso + Docker）→ `-boot`（QEMU 测）→ `-sign` → `-upload`。
ISO 内嵌离线包镜像，让装机不依赖网络。

**前置条件（当前都不满足）**：
1. 一台可 root 的 Linux 机器（装 archiso / docker / qemu）
2. 一台真实裸机做硬件兼容验证（NVIDIA 驱动、内核、UEFI、加密）
3. 外网（拉包、建仓库）
4. 硬件兼容矩阵（目前只验证过 RTX 3080 Laptop + WSL2）

---

## 4. 已经落地的部分（本轮）

| 机制 | 文件 | 状态 |
|---|---|---|
| 版本号 | `VERSION` + `src/nexus/version.py` | ✅ |
| 四通道 | `version.CHANNELS` + `nexus channel set/list/current` | ✅ |
| 迁移机制 | `migrations/*.sh` + `update.run_migrations()` | ✅ 已实测执行 |
| 单一更新入口 | `nexus update`（空间→快照→取产物→迁移→换基底→重启） | ✅ dry-run 实测 |
| 分层快照（回滚点） | `layering.snapshot()` 复用为 pre-update 快照 | ✅ |
| 产物打包 | `packaging/make-tarball.sh`（含 sha256） | ✅ 52KB |
| deb 打包 | `packaging/make-deb.sh`（系统级 profile） | ✅ 48KB |
| 发布流水线 | `bin/nexus-release`（build/test/sign/verify/upload/check） | ✅ |
| 单文件安装器 | `bin/nexus-install`（下载→校验→验签→安装） | ✅ |
| CI | `.github/workflows/ci.yml`（含纯逻辑断言，不依赖硬件） | ✅ |
| Release | `.github/workflows/release.yml`（tag 触发 → 打包 → 签名 → Release） | ✅ |

---

## 5. 发布前的硬性要求（`bin/nexus-release check`）

```
✓ VERSION 存在
✓ CHANGELOG 存在
✓ LICENSE 存在
! 不是 git 仓库          ← 必须解决
! 无 gh                  ← 本机无外网
✓ gpg 可用
✓ python3 >= 3.11
```

**发布前必须做的三件事**：
1. `git init` + 提交 + 推到 GitHub（仓库名建议 `nexusos/nexusos`）
2. 配好 GPG 密钥与 `secrets.GPG_PRIVATE_KEY` / `secrets.GPG_KEY_ID`
3. 确认 `NEXUS_REPO` / `NEXUS_BASE_URL` 与真实仓库地址一致

---

## 6. 下一阶段的三个决定（需要人来定）

| 决定 | 选项 | 我的建议 |
|---|---|---|
| 仓库名与归属 | `nexusos/nexusos` / 个人名下 | 先用个人名下，验证有人用再谈组织 |
| 硬件兼容矩阵 | 只支持 NVIDIA / 也支持 AMD | 先只支持 NVIDIA（本地推理刚需），明确写在 README 顶部 |
| 是否做 ISO | 现在做 / 有 10 个用户再做 | **等**。先把阶段 A/B 跑通，ISO 是最后一步 |

---

## 7. 参考

- Omarchy 主仓库：https://github.com/basecamp/omarchy
- Omarchy ISO 仓库：https://github.com/omacom-io/omarchy-iso
- Omarchy 包仓库：https://github.com/omacom-io/omarchy-pkgs
- 官方手册（51 章）：https://omarchy.org/manual/
- 更新通道：https://omarchy.org/manual/updates/
- ISO 构建说明（archiso + Docker + QEMU + rclone + GPG）：
  https://github.com/omacom-io/omarchy-iso#readme
