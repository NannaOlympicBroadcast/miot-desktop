# 米家桌面端 (MIoT Desktop) 


基于 **Electron + miot-kit** 的米家桌面客户端。提供图形界面用于：

- 🔐 **登录**：应用内完成小米账号 OAuth2 授权，凭证缓存于本地。
- 📡 **设备控制**：自动加载设备的 MIoT **SPEC** 功能定义，并生成对应的控制控件
  （开关、滑块、下拉选项、动作按钮），可实时读取与设置属性、执行动作。
- 🏠 **Miloco**：通过界面把小米官方的感知家庭网关
  [Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco) **直接安装并运行在本机 Windows 上**
  （裸机部署，不需要 Docker，也不需要 WSL），一键启动/停止，**自动把本机米家凭据注入给
  Miloco**（无需重新绑定账号），并在应用内预览 Miloco 的 Web 控制台（见下方「关于 Miloco」）。
- 🤖 **SSR 助手**：内置进 Python 运行时的 SSR minimal runtime 智能体作为主 Agent（已限定为智能家居
  管家人设，装载 Miloco 的设备/感知/任务/规则数据类型作为上下文），可在界面上配置模型、进行
  聊天（支持**粘贴图片 / 上传文件**，工具调用流式可视化）；Miloco 的家庭事件会进入 SSR 的事件
  总线供 bus event handler 处理，Miloco 的感知/规则引擎也可通过 `/miloco/webhook` 直接驱动同一个
  Agent（见下方「关于 SSR 助手」）。
- 🪟 **托盘驻留**：点击关闭按钮收起到系统托盘；托盘右键可「显示主界面 / 快捷控制面板 / 退出」。
- 🎛️ **快捷控制面板**：托盘弹出的小窗，可直接调节默认音箱音量、播放/暂停，并
  **非静默执行自然语言指令**。
- 🗣️ **小爱音箱**：应用内登录小米账号获取小爱语音识别 (ASR) / 语音播报 (TTS) 所需的
  passport Token（与设备控制的 OAuth2 登录是两套不同凭据），可从已有的米家音箱设备中选择
  一个，把新的语音识别结果实时推送到自定义 Webhook；同时提供接管播报测试，以及如何用独立的
  `xiaomi-speaker-sdk` 开发自己的小爱音箱机器人的图文指引（见下方「关于小爱音箱」）。
- ⚙️ **开机自启**：设置中可开启「开机自动启动」（登录后静默驻留托盘）。

## 架构

```
┌─────────────────────────┐        HTTP / WebSocket        ┌──────────────────────────┐
│  Electron 渲染进程 (UI)  │ ───────  127.0.0.1:<port> ───▶ │  Python 后端 (sidecar)    │
│  renderer/ (HTML/JS/CSS) │ ◀────────────────────────────  │  backend/server.py        │
└─────────────────────────┘                                 │  封装 miot_kit            │
            ▲                                                │  · 设备 / 家庭 / SPEC      │
            │ preload.js (contextBridge)                     │  · 小爱 ASR/TTS · Miloco   │
┌─────────────────────────┐                                 │  · SSR 助手 (/ws/agent)   │
│  Electron 主进程 main.js │  spawn ────────────────────────▶│                            │
│  · 启动并托管后端进程     │                                 └──────────────────────────┘
└─────────────────────────┘
```

主进程 `main.js` 启动 Python 后端，后端把实际监听端口通过 stdout
（`MIOT_BACKEND_PORT=<port>`）回传，渲染进程经 `preload.js` 拿到地址后直接访问本地 API。

## 前置条件

1. **已完成米家 OAuth 登录**：凭证缓存在 `~/.miot_cache`（由 `login_miot.py` 生成）。
   本应用直接复用该缓存，无需在应用内重新登录。
2. **Python**（含 miot_kit 依赖：aiohttp、cryptography、pydantic、aiofiles、aiocache、pyyaml 等）。
   发布版已内置 Python 运行时，用户无需自备（见「打包为独立 exe」）。
3. **Node.js / npm**（已用 Node 24 + Electron 33 验证）。
4. 想用「Miloco」标签页把 Miloco 装到本机的话，本机还需要 **Python ≥ 3.11 + Git**
   （Web 控制台另需 Node.js）——这三项是给 Miloco 自己用的，与上面第 2/3 条无关，
   界面会自检并给出下载链接，详见「关于 Miloco（Windows 裸机部署）」。

## 配置

通过环境变量覆盖默认值（默认值已适配本机）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MIOT_PYTHON` | `C:/ProgramData/miniconda3/python.exe` | Python 解释器路径 |
| `MIOT_KIT_PATH` | `D:\claw\xiaomi_home_bundle\miot_kit` | miot_kit 库所在目录 |
| `MIOT_CACHE_PATH` | `~/.miot_cache` | OAuth 凭证缓存目录 |
| `MIOT_CLOUD_SERVER` | `cn` | 云服务区域 |
| `MIOT_LANG` | `zh-Hans` | SPEC 翻译语言 |

## 运行

```bash
npm install        # 安装 Electron（首次）
npm start          # 启动应用
```

或双击 `start.cmd`。

## 使用

- **设备控制**：左侧选择设备 → 自动加载 SPEC → 右侧按服务分组显示属性/动作。
  - 可写 `bool` → 开关；带 `value-range` 的整数 → 滑块；带 `value-list` → 下拉框；
    其余 → 文本框。只读属性提供「读取」按钮，动作提供「执行」按钮。
  - 顶部「读取全部状态」一次性批量拉取所有可读属性的当前值。
- **Miloco**：切换到「Miloco」标签，见下方「关于 Miloco」。
- **SSR 助手**：切换到「SSR 助手」标签，见下方「关于 SSR 助手」。
- **小爱音箱**：切换到「小爱音箱」标签，见下方「关于小爱音箱」。

## 关于小爱音箱

小爱音箱的语音识别 (ASR) / 语音播报 (TTS) 使用的是小米账号的 **passport 登录凭据**
（`passToken`），与本应用其余功能使用的**米家开放平台 OAuth2** 登录是两套不同的凭据——
后者只能用于 SPEC 设备控制，无法用于小爱对话。这个 Token 通常需要在浏览器里完成一次登录
（可能包含短信/设备安全验证）才能拿到。

「小爱音箱」标签页做了三件事：

1. **提取 Token**：点击按钮后，`main.js` 会打开一个应用内的登录窗口（`xiaomi-passport-login`
   IPC handler）加载小米账号登录页；成功登录后直接从该窗口的 `session.cookies` 读取
   `passToken`/`userId`（Electron 原生支持读取 httpOnly cookie，不需要像纯 CLI 工具那样
   另外拉起浏览器 + DevTools Protocol 去抓包）。Token 保存在本地
   `~/.miot_cache/xiaomi_speaker/token.json`（后端 `xiaomi_asr_bridge.py`），与
   `~/.miot_cache` 下的 OAuth2 缓存互不影响。
2. **选择音箱 + 推送到 Webhook**：音箱直接从你**米家账号下已有的设备列表**中选（而不是一份
   陌生的原始小爱设备 ID 列表），选中后由后端按 MIoT DID 去匹配小爱账号下的对应设备；随后可
   配置一个 Webhook 地址，收到新的语音识别结果时实时 `POST` 一份 JSON 过去。也提供「接管播报
   测试」按钮，验证随时暂停播放、朗读任意文字的能力。
3. **SDK 开发指引**：页面底部有图文说明，指导如何把提取到的 Token 接到独立发布的
   [`xiaomi-speaker-sdk`](https://github.com/NannaOlympicBroadcast/ssr-agent/tree/main/xiaomi_speaker_sdk)
   包（同一套 `miservice` Token 格式，可直接复用 Token 文件）上，用几行代码监听 ASR / 接管
   播报，开发自己的小爱音箱机器人——不依赖本应用即可运行。

## 关于 Miloco（Windows 裸机部署）

[Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco) 是小米官方开源的「感知家庭」网关，
绑定米家账号后枚举设备/家庭/成员、记录活动事件、运行自动化，并在
`http://127.0.0.1:1810` 暴露 Web 控制台与 HTTP API。

本应用**直接把 Miloco 装到本机 Windows 上以普通进程运行**（早期版本走 Docker
`--network host`，现已完全移除）。

### 为什么以前必须用 Docker，现在不用了

小米官方的 Windows 安装脚本（`scripts/install.ps1`）会直接报错让你改用 WSL，原因只有一个：
`miloco-miot` 没有 Windows wheel，因为它随包分发的**摄像头 P2P 原生库只为 linux/darwin 构建**。
这不是可选项——`MIoTClient.init_async()` 会构造 `MIoTCamera`，其 `__init__` 无条件 `CDLL` 该库，
缺库就在客户端初始化阶段直接抛异常。

本仓库现在自带一份**为 Windows x86_64 自行编译的 `miot_camera_lite.dll`**
（`vendor/miot_kit/miot/libs/windows/x86_64/`，详见
[vendor/miot_kit/NOTICE.md](vendor/miot_kit/NOTICE.md)）。安装时它会被复制进 Miloco 自带的
`miot` 源码树再打包安装，Windows 裸机运行因此成立；同时本应用自己的摄像头实时画面解码也
不再走 no-op 降级桩。

### 前置条件（本机需要装的东西）

| 组件 | 是否必需 | 用途 |
| --- | --- | --- |
| Python ≥ 3.11 | **必需** | Miloco 是 Python 服务；安装器会用它建虚拟环境（可用 `MILOCO_PYTHON` 指定解释器） |
| Git | **必需** | 从官方仓库获取 Miloco 源码（支持分支/标签/commit） |
| Node.js（自带 npx）| 建议 | 构建 Miloco 的 Web 控制台。缺失时会跳过该步骤：HTTP API 正常，但控制台 `GET /` 返回 404（本页会明确提示），装好后点「重新安装 / 更新」即可补上 |

界面会自检这三项并给出下载链接；缺必需项时「安装」按钮不可点。pnpm 不需要单独安装——
优先用 PATH 上的 `pnpm`，否则用 `npx --yes pnpm@11`（不写全局 shim、不需要管理员权限）。

### 「安装 Miloco」这一步做了什么

后端 `miloco_controller.py` 逐步执行以下 8 步（界面上实时显示每步状态/耗时，可展开原始日志，
完整日志同时写到 `$MILOCO_ROOT/install.log`）：

1. 检测本机环境（Python / Git / Node·pnpm / 摄像头 DLL）
2. `git fetch --depth 1` 获取源码到 `$MILOCO_ROOT/src`（用 fetch 而非 `clone --branch`，
   所以 `MILOCO_REF` 可以是分支名、标签，也可以是完整 commit SHA）
3. 构建 Web 控制台（`web/` → `backend/miloco/src/miloco/static`，与上游
   `scripts/build.sh::build_web` 完全一致的产物清单与校验）
4. 把 Windows 摄像头原生库注入 `backend/miot/src/miot/libs/windows/<arch>/`
5. 把感知 ONNX 模型复制到 `$MILOCO_HOME/models`（模型在仓库里但被 wheel 排除，
   而 Miloco 从 `$MILOCO_HOME/models` 读取——这一步是 Docker 版本没有做、感知功能因此
   不可用的地方）
6. 创建虚拟环境 `$MILOCO_ROOT/venv`
7. 依次 `pip install backend/miot`、`backend/miloco`、`cli`（前两者顺序同镜像构建：先装
   `miloco-miot` 源码，`miloco` 的同名依赖才不会去找不存在的 Windows wheel。第三个是
   `miloco-cli`——SSR 助手正是通过它拿设备目录、家庭档案并发通知，缺了它助手就是瞎的）
8. 写入 **Windows 兼容层**（见下）并校验：在虚拟环境里实际执行一次
   `import miloco.main`（Windows 上另加 `os.sysconf` / `zoneinfo` 检查）+ 记录
   `$MILOCO_ROOT/install.json`

网络受限时可在「镜像源设置」里选 PyPI / NPM 镜像（选择保存在本机，下次沿用）。首次安装需要
下载数百 MB（源码含 79 MB 感知模型 + 依赖 wheel），通常几分钟。

> 第 8 步为什么要真的 import 一遍：Miloco 至今遇到的每个 Windows 不兼容点都发生在
> **导入期**（见下），而不是运行期。把 `import miloco.main` 放进安装校验，等于把
> 「安装成功、一启动就崩」变成「安装失败并附上 traceback」。

### Windows 兼容层

上游只支持 Linux/macOS（安装脚本直接拒绝 Windows），因此有几处 POSIX-only 的标准库调用是
无条件执行的，且都在 `import` 期就触发——后端来不及打任何日志就退出。安装器会往虚拟环境的
site-packages 写入两样东西（**不改上游源码**，因为改了会被下一次更新覆盖），并在**每次安装和
每次启动时**重写，所以旧版本装出来的环境无需重装即可获得新补丁：

| 文件 | 补什么 | 为什么必须 |
| --- | --- | --- |
| `fcntl.py` | Windows 完全没有的 `fcntl` 模块；`flock()` 映射到 `msvcrt.locking()` 的字节区间锁（真锁，非空实现） | `miloco.home_profile.store` 顶层 `import fcntl` 做跨进程文件锁，而 `miloco.main` 会导入它 |
| `miot_desktop_win_compat.py` + `zz-miot-desktop-win-compat.pth` | `os.sysconf`（只回答 `SC_PAGESIZE` / `SC_PAGE_SIZE` / `SC_NPROCESSORS_*`，取真实值；其余按 CPython 语义抛 `ValueError`） | `miloco.node_monitor.vmmap` 在模块顶层求值 `os.sysconf("SC_PAGESIZE")`，而 `resource_monitor` 无条件导入它（那是 macOS-only 代码，平台分发发生在调用时）→ `AttributeError: module 'os' has no attribute 'sysconf'` |

`.pth` 的作用是让 site.py 在**解释器启动时**就执行补丁，早于任何 miloco 模块导入——这也是唯一
可行的时机。补丁只在有真实 Windows 答案时回答，绝不把失败伪装成成功。

**写了 ≠ 生效**，所以写完还会校验：用虚拟环境自己跑一次
`import fcntl, miloco.node_monitor.vmmap`。

- 校验通过 → 正常启动。
- 校验失败（说明该环境没有加载 `.pth` 启动钩子）→ 自动回退：直接把已安装的
  `vmmap.py` 那一行改成 `os.sysconf(...) if hasattr(os, "sysconf") else mmap.PAGESIZE`，
  清掉过期的 `__pycache__` 后再校验一次。（这是兜底而非首选：改动会被下次更新覆盖，
  所以优先用 `.pth`。若上游改了那一行，则**不做**盲目替换，让真实错误暴露出来。）
- 两次都失败 → 「启动」直接报错并说明原因，而不是启动一个必然崩溃的进程；界面也会显示
  横幅提示。

### 已经装好、但还在旧版应用上的修复办法

兼容层由应用后端写入，所以**必须先更新应用本身**（开发模式 `git pull` 后重启应用即可；
打包版需要重新构建 exe）。如果暂时不方便更新，可以直接跑仓库里的独立脚本，它不依赖应用、
只操作已安装的虚拟环境：

```powershell
git pull
python scripts\apply_win_compat.py
# 或指定虚拟环境位置
python scripts\apply_win_compat.py --venv "D:\path\to\miloco_runtime\venv"
# 也可以用等价的 PowerShell 包装
.\scripts\repair_miloco_win.ps1
```

脚本会**从 `backend/miloco_controller.py` 里解析出**这两个补丁的源码（因此与应用自己写入的
内容逐字节一致，不存在第二份会走样的副本），写入虚拟环境，然后用该虚拟环境实际
`import miloco.main` 验证；`.pth` 钩子不生效时同样会自动回退修补 `vmmap.py`。成功后回到应用
点「启动 Miloco」即可。

> `.ps1` 只是个瘦包装：Windows PowerShell 5.1 对 LF 换行的 here-string 会解析错位，对无 BOM
> 文件又按 ANSI 读取（中文会乱码），所以脚本本体是 Python，`.ps1` 保持纯 ASCII + CRLF
> （仓库用 `.gitattributes` 固定 `*.ps1 text eol=crlf`）。

内存 region 采集（`/proc/self/smaps`）在 Windows 上不可用，Miloco 自己会在启动探测时捕获并标记
`memory regions not available`，其余监控正常——这与容器行为一致，属于预期降级。

### 时区

Miloco 只从 `TZ` / `/etc/timezone` / `/etc/localtime` 推断部署时区（全是 Unix 路径），推断失败就
兜底 `Asia/Shanghai`。所以在 Windows 上，**所有时间戳本来会一律按中国时区显示**。启动时本应用会
问虚拟环境里的 `tzlocal`（`apscheduler` 的依赖，Windows 上它还会带上 `tzdata`，这也是 Windows
`zoneinfo` 能用的前提）拿到真实 IANA 时区名，作为 `MILOCO_TIMEZONE` 传给 Miloco；结果缓存在
`install.json`。若你已在 `config.json` 里显式写了 `timezone`，则**不覆盖**你的配置。

### 运行与状态

- **启动**：先刷新一次 Windows 兼容层（幂等，内容未变则不写盘），再直接拉起
  `$MILOCO_ROOT/venv/Scripts/miloco-backend.exe`，env 里指定 `MILOCO_HOME`、
  `MILOCO_SERVER__PORT`/`MILOCO_SERVER__URL`、`MILOCO_TIMEZONE`；进程 stdout/stderr 落到
  `$MILOCO_HOME/backend.log`，pid 记到 `$MILOCO_ROOT/miloco.pid`。
- **重启本应用后**：会通过 pid 文件**重新接管**仍在运行的 Miloco（并校验该进程确实来自我们的
  虚拟环境，避免 pid 复用误判），不会重复启动第二个实例。
- **崩溃处理**：与 `docker run --restart unless-stopped` 不同，**不会自动重启**——界面会显示退出码
  与日志尾部，由你决定是否重新启动（宁可把启动失败暴露出来，也不做隐式重试把问题掩盖成
  「正在启动…」）。
- **查看日志**：同时展示 Miloco 自己的服务日志（`$MILOCO_HOME/log/miloco-backend_*.log`）与我们
  捕获的进程输出（启动即崩溃时的 traceback 只会出现在后者）。

### 自动注入凭据与 Web 预览（与 Docker 版本一致）

1. **自动注入凭据**：Miloco 把米家 OAuth Token 存在其 SQLite `kv` 表的
   `MIOT_TOKEN_INFO_KEY` 键下（`MIoTOauthInfo` 的 JSON）；本应用在 `~/.miot_cache` 里缓存的
   正是**同一种** `MIoTOauthInfo`（两边都用上游 `miot` 库，字段完全一致）。因此启动前后端会把
   本机 Token 直接写进 Miloco 的数据库（`$MILOCO_HOME/miloco.db`，默认在
   `~/.miot_cache/miloco_home/`），Miloco 启动即已绑定，无需再走一次 OAuth。
2. **Web 预览**：Miloco 就绪后，用 Electron `<webview>` 内嵌其控制台（token 由 Miloco 首次
   启动时生成并由其自己注入页面）。

### 相关环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MILOCO_PYTHON` | 自动探测（`py -3.13/-3.12/-3.11`、`python`…） | 用于建虚拟环境的解释器 |
| `MILOCO_ROOT_HOST` | `~/.miot_cache/miloco_runtime` | 安装目录（`src/` + `venv/`），与状态目录分开，重装不动状态、清状态不坏安装 |
| `MILOCO_HOME_HOST` | `~/.miot_cache/miloco_home` | Miloco 状态目录（`config.json` / `miloco.db` / `log/` / `models/`） |
| `MILOCO_PORT` | `1810` | Miloco 监听端口 |
| `MILOCO_REPO` / `MILOCO_REF` | 官方仓库 / `main` | 安装来源与版本（分支/标签/commit） |
| `MIOT_CAMERA_DLL` | 内置路径 | 指定摄像头原生库位置（自行编译时用） |

## 关于 SSR 助手

[SSR minimal runtime](https://github.com/NannaOlympicBroadcast/ssr-minimal) 是一个基于
Google ADK 的 code-driven agent 运行时，被**内置进本应用的 Python 运行时**作为驱动 Miloco 的主
Agent（后端 `ssr_agent_bridge.py`）。相比早期基于完整 `ssr-agent`（CLI/TUI + IM 渠道 + 插件系统
+ 云配对）的实现，minimal runtime 只保留 agent 循环、上下文池、事件总线与 SDK，打包依赖显著更少
（无 `rich`/`pyfiglet`/`prompt_toolkit` 等 CLI/TUI 依赖，见 `miot-backend.spec`），Python 包名
仍是 `ssr`，`ssr_agent_bridge.py` 的调用方式不变。「SSR 助手」标签页：

1. **模型配置**：读写 SSR 自己的 `~/.ssr/models.json` 与 `~/.ssr/.env`，可设置主模型
   （provider / 模型名 / API Key / Base URL）。
2. **聊天**：通过 `/ws/agent` WebSocket 与 `SSRAgent` 对话，实时 streaming
   `turn_start`/`thinking`/`tool_call`/`tool_result`/`reply` 事件（工具调用/返回值在界面上以独立卡片
   实时展示）。支持**粘贴图片**（作为多模态图片输入）与**上传文件**（保存到 Agent 工作目录，供其
   用文件工具读取）。
3. **智能家居人设 + Miloco 数据类型**：`ssr.integrations.miloco.apply_smart_home_scope` 把 Agent
   人设限定为智能家居管家，并把 Miloco 的设备目录 / 感知事件 / 任务 / 规则数据类型作为 embedded
   context 装载，使其只处理智能家居相关请求。
4. **Miloco 工具装载**：`ssr.integrations.miloco:MilocoTools` 会被登记进 `settings.agent_tools`，
   助手因此真正**拥有** `miloco_im_push` / `miloco_notify_bind` / `miloco_notify_unbind` /
   `miloco_habit_suggest` 四个工具（上一条的 `apply_smart_home_scope` 只提供提示词素材——它告诉
   模型这些工具存在，却不提供工具本身；只做前者的话，小模型会“知道有 `miloco_im_push`”，然后用
   `run_command echo` 假装查询并编造结果）。
   同时后端会把 `MILOCO_HOME` / `MILOCO_SERVER_URL` / `MILOCO_SERVER_TOKEN` / `MILOCO_CLI_BIN`
   导出到自身进程，并把 Miloco 虚拟环境的 `Scripts` 目录加进 `PATH`——因为
   `ssr.integrations.miloco` 一律通过 `MilocoConfig.from_env()` 与 PATH 上的 `miloco-cli`
   访问 Miloco，而它默认找的是 `~/.openclaw/miloco`、且 `miloco-cli` 装在 Miloco 自己的虚拟环境里
   （两者都不匹配本应用的裸机安装，于是设备目录 / 家庭档案 / 工具调用会**静默**退化为空）。
   「SSR 助手」标签页顶部会显示当前工具装载与 Token 注入状态，装不上时给出明确提示而不是让模型
   自由发挥。
5. **Miloco ⇄ 总线 / Webhook 双向打通**：启动时挂载 `ssr.integrations.miloco.start_bus_bridge`——
   一边把 Miloco 的连通性变化投递到 SSR 事件总线供 bus event handler 响应，一边在 Miloco 默认的
   agent webhook 端口（`18789`）启动 `/miloco/webhook` 接收端；`miloco_controller.py` 启动 Miloco
   进程时会把该地址与一个共享 bearer token 写入 Miloco 的 `config.json::agent`，让 Miloco 的感知/
   规则引擎可以直接调用同一个 Agent（本应用的 `/miloco/webhook` REST 路由是等价的备用入口）——这就
   是「自动化事件让 SSR 助手直接处理」的落地。

未在后端 Python 环境安装 SSR minimal runtime 时，该标签页会给出安装指引而不会影响其它功能
（可 `pip install ssr-agent-minimal`，或设置 `SSR_AGENT_PATH` 指向其源码目录）。

## 打包为独立 exe（内置 Python 运行环境）

发布版**无需用户安装 Python**：用 PyInstaller 把后端（含 Python 运行时、
内置的 `miot` 包及其依赖）冻结成 `miot-backend.exe`，再由 electron-builder 把
Electron 应用与该后端打包到一起。生产环境下 `main.js` 启动的是
`resources/backend/miot-backend.exe` 而非系统 Python。

本地手动打包（Windows）：

```bash
pip install -r backend/requirements.txt
pyinstaller backend/miot-backend.spec --noconfirm --distpath dist-backend --workpath build-backend
npm install
npm run dist          # 产物在 dist/：安装包(nsis) 与 便携版(portable)
```

## CI / 自动发布

`.github/workflows/build.yml` 在**推送 `v*` 标签**（或手动触发）时于
`windows-latest` 上构建并把 exe 上传到 GitHub Release：

```bash
git tag v1.0.0
git push origin v1.0.0      # 触发构建 + 发布 Release
```

## 关于内置的 miot_kit

`vendor/miot_kit/miot/` 是内置的小米 miot-kit（详见 [vendor/miot_kit/NOTICE.md](vendor/miot_kit/NOTICE.md)），
仅为 CI 能构建出独立程序而内置；其版权与授权归 Xiaomi。未包含上游随库分发的 Linux/macOS 摄像头
原生库；`miot/libs/windows/x86_64/miot_camera_lite.dll` 是**自行为 Windows 编译**的同一份上游摄像头库
（上游从不分发 Windows 版本），它同时支撑本机摄像头画面解码与 Miloco 的 Windows 裸机部署。

## 目录结构

```
miot-desktop/
├── package.json          # Electron 配置 + electron-builder 打包配置
├── main.js               # 主进程：启动后端(dev=python / prod=exe) + 窗口 + 托盘 + 自启
├── preload.js            # contextBridge 暴露后端地址与 IPC
├── backend/
│   ├── server.py           # aiohttp 后端，封装 miot_kit（登录/设备/SPEC + 小爱/Miloco/SSR 路由）
│   ├── xiaomi_asr_bridge.py # 小爱音箱 passport 登录 + ASR 轮询 + Webhook 推送 + TTS 接管
│   ├── miloco_controller.py # Miloco 的 Windows 裸机安装/启停 + 米家凭据注入 + 健康检查
│   ├── ssr_agent_bridge.py  # 内置 SSR 智能体：模型配置 + 聊天(图片/文件) + Miloco→总线
│   ├── requirements.txt
│   └── miot-backend.spec   # PyInstaller 打包脚本
├── scripts/
│   ├── apply_win_compat.py        # 独立修复：给已装好的 Miloco 虚拟环境补 Windows 兼容层
│   └── repair_miloco_win.ps1      # 上面脚本的 PowerShell 包装（纯 ASCII + CRLF）
├── renderer/
│   ├── index.html / styles.css / app.js   # 主界面：设备、Miloco、SSR 助手、小爱音箱、设置
│   └── quick.html / quick.js              # 托盘快捷控制面板
├── vendor/miot_kit/miot/ # 内置的 miot 库（供打包）
│   └── libs/windows/x86_64/miot_camera_lite.dll  # 自行编译的 Windows 摄像头原生库
├── build/icon.ico        # 应用图标
├── assets/tray.png       # 托盘图标
└── .github/workflows/build.yml  # CI：构建 exe 并发布 Release
```
