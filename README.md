# PTFlow · 飞牛原生 PT 刷流管理器

面向 **fnOS 1.1.3100+** 的独立原生应用，连接 M-Team 与 qBittorrent，通过飞牛桌面管理查询、刷流、安全维护、趋势和实时日志。中文界面、明暗主题、移动端布局；资源全部随包交付，不加载 CDN。

**不需要 Docker、系统 Python、Node.js 或启动时安装依赖。** 每个架构包内置固定版本的便携 CPython 与全部 Python 依赖。PTFlow 不提供站点账号、API Key、种子或流量承诺，请遵守站点规则与当地法律。

[下载正式安装包](https://github.com/mhduiy/ptflow-fnos/releases) · [构建记录与开发版产物](https://github.com/mhduiy/ptflow-fnos/actions) · [问题反馈](https://github.com/mhduiy/ptflow-fnos/issues) · [更新记录](CHANGELOG.md)

## 支持范围

| 项目 | 说明 |
| --- | --- |
| 飞牛系统 | fnOS **1.1.3100 或更新版本**，使用原生应用统一网关 |
| Intel / AMD NAS | `PTFlow_1.0.0_x86_64.fpk`，基线 x86_64，不要求 AVX |
| ARM NAS | `PTFlow_1.0.0_arm64.fpk`，aarch64 / arm64 |
| 站点 | M-Team API；默认 `https://api.m-team.cc` |
| 下载器 | qBittorrent Web API；用户名/密码认证，或支持 API Key 的版本（通常为 5.2.0+） |
| 种子格式 | BEP 3 / v1；包含 v1 infohash 的混合种子可用，纯 BitTorrent v2 明确拒绝 |
| 运行环境 | 包内 CPython 3.12.16、Flask、requests、waitress、SQLite；Linux glibc 运行时与 pidfd 内核支持 |

只提供架构专用离线包，不提供下载运行时的通用包。这里的“离线包”表示安装与启动无需拉取代码/依赖；连接站点和下载器仍需要网络。不要把 ARM 包安装到 x86 NAS，反之亦然。

## 安装与升级

1. 在 Releases 下载适合 CPU 的 `.fpk` 及同名 `.fpk.sha256`。
2. 可选但推荐：在下载目录校验完整性。Linux 使用 `sha256sum -c PTFlow_1.0.0_x86_64.fpk.sha256`；macOS 使用 `shasum -a 256 -c PTFlow_1.0.0_x86_64.fpk.sha256`。ARM 请替换文件名。SHA-256 检查下载损坏，不代替对发布者的信任。
3. 打开 **飞牛 → 应用中心 → 手动安装**，选择 `.fpk` 并完成安装。
4. 从飞牛桌面打开 **PTFlow**，入口为 `/app/ptflow-fnos/`，无需另开 NAS TCP 端口。
5. 首次初始化必须从 **飞牛管理员桌面**打开，设置一个 **至少 10 字符**的独立管理密码；该密码不是下载器密码。飞牛网关确认管理员身份后允许初始化，此后仍需 PTFlow 自己的密码登录。随后完成下方连接设置。

升级时在应用中心直接手动安装新版同架构 FPK；生命周期先停止旧进程，应用数据保留并继续使用。没有应用内自动更新器，不会自行拉取或替换安装包。

> 本仓库包含真实服务/模拟 HTTP 联调与包结构检查入口；**不能把这些检查等同于真机安装测试**。没有可用 NAS 时，不能声明已在真实 fnOS 上验证安装。具体已运行检查、GitHub 构建成功与否，以对应提交的 Actions 和交付记录为准。

## 首次设置

### 1. 连接 qBittorrent

在“设置 → 下载器”填写：

- **WebUI 地址**：从 NAS 主机可访问的完整 HTTP(S) 地址，如 `http://192.168.1.20:8080`。PTFlow 原生运行在主机上；如果 qBittorrent 是容器，`127.0.0.1` 指主机，必须使用容器发布到主机的端口，不能直接使用仅容器网络可解析的服务名。
- **认证方式**：用户名/密码，或下载器实际支持的 API Key。两者按所选方式生效。
- **下载保存路径**：可选，填写 **qBittorrent 所在环境**内的路径；不是 PTFlow 的目录。留空使用下载器默认路径。PTFlow 通过下载器 API 工作，不直接访问下载目录。
- 点击“测试连接”，查看实际返回结果后保存。连接测试也支持尚未保存的表单；留空的密码或 API Key 保留已有值，保存不会把现有密钥清空。

不要通过关闭下载器认证、关闭 TLS 验证或面向公网开放 WebUI 来绕过错误。使用 HTTPS 时证书必须有效；自签名证书应先通过受信任网络正确部署，而不是忽略验证。

### 2. 连接 M-Team

填写站点 API 地址及个人 API Key，选择查询分类，设置请求间隔后“测试连接”。默认查询分类为电影、电视剧、普通；**默认相邻站点请求间隔 60 秒**。三个分类串行查询时，等待可能累计两分钟以上，界面会显示当前阶段；这不是卡死，不要反复提交任务。

地址不可含用户名密码、查询参数或片段；M-Team 使用 HTTPS（本机测试服务例外）。API Key 会持久保存但不回显。测试成功仅证明这次连接可用，不代表 Free 候选一定可下载，也不代表 qBittorrent 已接收任务。

### 3. 检查策略，再手动运行

推荐先保持“自动刷流”关闭，确认剩余空间、同时下载数、上传目标、下载上限和 Free 保护时间，再点“立即执行一次”。完整任务按 **查询 → 采样 → 刷流 → 安全维护** 执行。手动任务忽略自动开关与工作时段，**不会跳过磁盘、速度、数量或 Free 安全门槛**。已有任务执行时拒绝重复执行，不堆积后台队列。

确认日志与下载器中的实际结果后，才开启自动刷流。下载速度、上传贡献与分享率受资源热度、网络和站点规则影响，不保证新增任务或达到目标速度。

## 调度与单位

| 设置 | 默认值 | 行为 |
| --- | --- | --- |
| 自动刷流 | **关闭** | 配置完整并确认策略后手动开启 |
| 查询周期 | 30 分钟 | 从站点更新 Free 候选，不等于已经添加到下载器 |
| 刷流周期 | 15 分钟 | 重新读取下载器状态并补充符合门槛的任务 |
| 安全维护周期 | 10 分钟 | Free 窗口保护；空闲清理仍须单独启用 |
| 状态采样 | 30 秒 | 有下载器配置后持续采样，并检查已归属任务的 Free 到期保护，包括自动刷流暂停期间 |
| 工作时段 | `00:00`–`00:00` | 起止相同表示全天；支持跨午夜，例如 `22:00`–`06:00` |
| 时区 | `Asia/Shanghai` | 包内 tzdata，不依赖 NAS 恰好安装该时区数据 |
| 相邻站点请求间隔 | 60 秒 | 只在实际相邻请求间等待，与查询周期不同 |
| 磁盘保留空间 | 50 GiB | 指下载器返回的可用空间，不是 PTFlow 数据目录空间 |
| 同时下载上限 | 4 | 新增前检查实际下载任务数量 |
| 上传目标 / 下载上限 | 2 / 20 MiB/s | 上传按 10 分钟窗口均值，下载按新鲜状态门槛判断 |
| Free 剩余保护时间 | 3 小时 | 过期或无法确认结束时间的资源不新增 |
| 单任务下载容量 | 10 GiB | 可选择完整文件子集；连一个完整文件都放不下则跳过 |
| 空闲清理 / 删除文件 | **关闭 / 关闭** | 两个独立开关，默认不删除种子文件 |
| 空闲阈值 / 最短保种 | 60 分钟 / 24 小时 | 仅适用于已确认归属的任务，并排除正在传输的任务 |

修改配置立即重新安排下一次任务，不需要重启。页面显示当前阶段、上次结果和下一次计划；任务运行过久时不会同时启动另一项写操作。趋势图和累计上传/下载是 **整个 qBittorrent 的全局数据**，不应当误认为仅 PTFlow 管理种子的贡献；管理种子表单独显示任务信息。离线、无采样或旧采样会诚实显示空状态/更新时间，不使用演示数据。

## 数据保护与任务归属

- 每个候选资源先确认 Free 及有效截止时间，再检查下载器当前状态、容量、速度、数量、做种者/下载者条件。站点查询成功不等于任务添加成功。
- 通过真实 torrent `info` 原始字节计算 v1 infohash；新增时先停止，选择完整文件、读取回确认后才恢复，避免大包先下载再拆分。
- 只管理 **分类 `ptflow` 且数据库已持久确认对应 hash** 的任务。仅手工改成同名分类不足以被 PTFlow 接管；既有 `ptbrush`、手工种子和其他分类不迁移、不操作。
- 添加 `ptflow-id-<站点ID>` 标签用于不确定结果的恢复确认。API 接收与真实下载器列表确认是两件事；结果不确定时记录 `uncertain` 并先对账，**不会盲目再次发送新增请求**。
- 429 / Retry-After、认证错误、网络错误、磁盘不足、速度已达目标等会有明确记录。429 可导致该候选失败并继续处理后续候选，不能据此假定下载成功。
- 接近 Free 保护窗口时禁用未完成文件的继续下载，保留文件与可做种数据；这一保护不依赖空闲删除开关。**暂停自动刷流只停止自动查询、刷流补充与空闲清理；持续采样仍检查已归属任务的 Free 到期保护。** `cleanup_enabled=false` 仅禁止空闲删除，不关闭 Free 优先级保护。站点可能随时变更优惠规则，不能把任何策略视为免计下载承诺。
- 空闲清理须单独启用，满足保护时长且没有正在上传/下载才允许处理；删除实际下载文件必须再明确开启 `delete_files`。**开启前请充分理解站点保种要求，建议先保持关闭。**
- Web 管理需要独立密码；会话和同源 CSRF 检查覆盖修改操作。API 密钥/密码不回显，日志会脱敏，但种子名称、站点信息、路径仍可能包含隐私。
- 原生 Web 进程以专用普通用户 `ptflow-fnos` 运行；root 只负责目录准备和受锁保护的生命周期。进程记录同时校验 UID、可执行文件、完整命令与 Linux 启动时间，并通过 pidfd 发信号，拒绝误杀复用 PID 的其他进程。

不要直接暴露应用或下载器到公网；优先经飞牛认证入口和受信任局域网使用。SQLite、会话密钥及下载器/站点凭证都在应用数据目录内，需保护备份。凭证是本地持久保存而非加密保险箱；拥有 NAS root 权限的人仍可读取。

## 日志、数据与卸载

持久目录由飞牛分配，**不硬编码存储卷**：

```text
${TRIM_PKGVAR}/
├── data/              # 0700；配置、会话信息、SQLite 数据库
│   └── ptflow.sqlite3 # 候选、确认归属、任务、样本、结构化事件；WAL 模式
├── ptflow.log         # 0600；服务启动输出与真实错误，每次启动检查大小
├── ptflow.log.old     # 最多一份 1 MiB 的旧启动日志
└── ptflow.sock        # 原生 HTTP Unix socket，不是数据文件
```

包文件与 Python 位于 `${TRIM_APPDEST}`；`app.sock` 是链接到真实 socket 的网关入口。root 生命周期锁及可信进程记录位于 `/run/ptflow-fnos/`，重启后不拿旧 PID 直接发信号。运行时 `/health` 的实际 HTTP 200 响应才算就绪，非“创建了 PID 文件就启动成功”；停止先 TERM，最多等 30 秒，超过截止时间才强制终止本应用进程。

“运行日志”每 **2 秒**按事件游标增量读取，支持级别/阶段/关键字/任务筛选、暂停、自动滚动与导出。结构化事件保留约最近 **10,000 条**（每 100 条整理一次，最多 10,099 条），状态样本保留 **7 天**，任务历史保留最近 **200 次**；旧事件淘汰会显示提示，导出仅包含仍保留的脱敏记录。原生模式的重复 HTTP / 采样事件只入 SQLite，不重复追加到标准输出文件。启动失败或页面打不开时，在飞牛应用中心查看错误，再检查 `${TRIM_PKGVAR}/ptflow.log`；每次启动时，若文件达到 1 MiB，会将最新 1 MiB 保存为唯一一份 `ptflow.log.old` 并清空当前文件。生命周期错误交给飞牛错误输出文件，每次错误覆盖一条，不额外维护无界日志；本地开发模式仍会同步输出结构化日志到终端。

正常成功采样只保留在趋势与事件日志中，不占用任务历史，避免淹没手动执行结果；采样失败仍进入任务历史。

**备份**：先在应用中心停止 PTFlow，再完整复制 `${TRIM_PKGVAR}/data/`，不要在 SQLite 正写入时只拷贝主数据库、遗漏 `-wal` 文件。恢复到同一应用的数据目录后再启动；不要把旧 PTBrush 的数据库/配置直接覆盖进去，两个应用的数据模型独立。

**卸载**：默认“仅卸载应用，保留数据”。配置、密码、候选、日志和归属确认仍在原应用存储空间，重新安装同应用时沿用。只有在 **飞牛卸载向导明确选择“删除 PTFlow 应用数据”** 后才删除本应用数据与日志；卸载脚本不连接 qBittorrent，不移除下载器种子或文件。保留数据的重新安装请继续选择原存储空间；换卷不会自动迁移旧目录。

## 本地开发与构建

本地开发只要求 Python 3.12+；首次依赖安装发生在开发/构建机器上：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r requirements.lock
python -m unittest discover -s tests -p 'test_*.py' -v
python -m unittest discover -s scripts -p 'test_*.py' -v
python -m ptflow --data-dir "$HOME/.local/share/ptflow-dev" --host 127.0.0.1 --port 8787
```

打开 `http://127.0.0.1:8787/`。这是回环开发入口，不是 NAS 公网部署方法。`SOCKET_PATH` 环境变量存在时优先使用 Unix socket；原生模式使用 `GATEWAY_PREFIX=/app/ptflow-fnos`，兼容飞牛保留或剥离前缀两种转发方式。

FPK **必须在与目标架构相同的原生 Linux 机器上**构建；不在 macOS 上运行 Linux Python，也不通过 Docker、QEMU 或交叉执行来伪装 native 构建：

```bash
# Intel / AMD Linux
./scripts/build-manual.sh x86_64
# ARM64 Linux
./scripts/build-manual.sh arm64
# 检查实际产物（嵌套 MD5、外层 SHA-256、ELF 架构、依赖、入口和图标）
python3 scripts/audit-fpk.py dist/*.fpk
```

构建脚本要求宿主 Python 3.12+ 和网络；从 Astral 官方 [python-build-standalone 固定发布 20261026](https://github.com/astral-sh/python-build-standalone/releases/tag/20261026) 下载 `install_only_stripped` CPython 3.12.16，**按官方发布资产的 SHA-256 校验后才解包执行**；地址、完整哈希及发布摘要来源记录于 [`scripts/runtime.lock.json`](scripts/runtime.lock.json)。NAS 启动时不下载任何运行时。所有应用依赖在构建时从带完整哈希的 `requirements.lock` 安装进该运行时；GPL、CPython、运行时组件与 wheel 自带许可文本随包交付。

构建临时目录自动清理；缓存只在构建机 `.cache/runtime/`。图标由构建脚本用 Python 标准库原创绘制并编码为 64/256 PNG，**不引入 Pillow/前端构建依赖，不从其他应用复制图标**。应用内容作为 `app.tgz` 的根目录打包，清单 `checksum` 填其 MD5；外层按参考 FPK 格式 gzip tar，并生成 `.fpk.sha256`。MD5 仅为飞牛包格式字段，下载完整性使用外层 SHA-256。

GitHub Actions 在推送 `main`、推送 `v*` 标签或手动触发时，分别使用 `ubuntu-24.04`（x86_64）与 `ubuntu-24.04-arm`（原生 ARM64）执行 unittest、构建和产物审计，再实际启动包内普通用户服务，检查 Unix 网关、认证、socket 权限、停止/重启与配置保留。`main` / 手动构建产物在 Actions Artifacts；匹配包版本的 `v*` 标签两架构都成功后，将 `.fpk` 和 `.sha256` 上传 Releases。发布需要仓库允许 workflow 的 `contents: write` 权限；不需要 Docker Hub 或运行时安装凭据。原生生命周期联调入口为 `scripts/smoke-fpk.py`，仅在隔离 Linux 构建机上以 root 执行，不代替飞牛真机安装验证。

## 致谢与许可证

PTFlow 使用 **[GPL-3.0-only](LICENSE)** 发布。其刷流思路与 M-Team / qBittorrent 行为参考 [PTBrush](https://github.com/mhduiy/ptbrush)（GPL-3.0；原项目 [lalaking666/ptbrush](https://github.com/lalaking666/ptbrush)），原生清单、统一网关、普通用户启动及手动 FPK 布局参考 [Clash-for-fnos](https://github.com/chenpingonline/Clash-for-fnos)（GPL-3.0）。感谢原作者与维护者。

本项目是独立新应用，不依赖旧 PTBrush checkout，不共享其配置/数据库，也未复制 Clash 的界面或图标。完整项目源码在本仓库公开；分发修改版本须遵守 GPL-3.0 的对应源码与版权要求。CPython / portable runtime 及 Flask、requests、waitress 等依赖保持各自许可证，许可和来源在 FPK 的 `licenses/` 与 Python 的 `*.dist-info/` 中保留。没有对 fnOS、M-Team 或 qBittorrent 官方合作/授权的声称，也不对刷流收益、站点规则变化或账号风险作保证。
