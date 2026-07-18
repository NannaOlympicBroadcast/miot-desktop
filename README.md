# 米家桌面端 (MIoT Desktop) 


基于 **Electron + miot-kit** 的米家桌面客户端。提供图形界面用于：

- 🔐 **登录**：应用内完成小米账号 OAuth2 授权，凭证缓存于本地。
- 📡 **设备控制**：自动加载设备的 MIoT **SPEC** 功能定义，并生成对应的控制控件
  （开关、滑块、下拉选项、动作按钮），可实时读取与设置属性、执行动作。
- 📷 **摄像头**：列出账号下的米家摄像头设备，并通过 P2P 原生库拉取实时画面
  （见下方「关于摄像头」）。
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
            │ preload.js (contextBridge)                     │  · 属性 get/set · action   │
┌─────────────────────────┐                                 │  · 摄像头 WebSocket 流     │
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
3. **Node.js / npm**（已用 Node 24 + Electron 33 验证）。

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
- **摄像头**：切换到「摄像头」标签查看设备列表，点「播放」拉流。
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

## 关于摄像头实时画面

miot-kit 的摄像头实时画面依赖一个平台相关的 P2P 原生库
`miot_camera_lite`（位于 `miot_kit/miot/libs/<平台>/`）。仓库仅自带
**linux / macOS** 版本，**不含 Windows DLL**。

因此在 Windows 上：

- 摄像头设备会正常列出，并可在「设备控制」中通过 SPEC 控制；
- **实时视频无法解码**，界面会给出明确提示。

后端已实现完整的 WebSocket 推流管线（解码 JPEG 帧 → 推送到渲染进程），
一旦把对应平台的 `miot_camera_lite.dll` 放入
`miot_kit/miot/libs/windows/x86_64/`，应用会自动检测并启用实时画面，无需改代码。

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
仅为 CI 能构建出独立程序而内置；其版权与授权归 Xiaomi。未包含上游的摄像头原生库二进制。

## 目录结构

```
miot-desktop/
├── package.json          # Electron 配置 + electron-builder 打包配置
├── main.js               # 主进程：启动后端(dev=python / prod=exe) + 窗口 + 托盘 + 自启
├── preload.js            # contextBridge 暴露后端地址与 IPC
├── backend/
│   ├── server.py           # aiohttp 后端，封装 miot_kit（含登录/设备/SPEC/摄像头/小爱音箱路由）
│   ├── xiaomi_asr_bridge.py # 小爱音箱 passport 登录 + ASR 轮询 + Webhook 推送 + TTS 接管
│   ├── requirements.txt
│   └── miot-backend.spec   # PyInstaller 打包脚本
├── renderer/
│   ├── index.html / styles.css / app.js   # 主界面：登录、设备、SPEC 控件、摄像头、小爱音箱、设置
│   └── quick.html / quick.js              # 托盘快捷控制面板
├── vendor/miot_kit/miot/ # 内置的 miot 库（供打包）
├── build/icon.ico        # 应用图标
├── assets/tray.png       # 托盘图标
└── .github/workflows/build.yml  # CI：构建 exe 并发布 Release
```
