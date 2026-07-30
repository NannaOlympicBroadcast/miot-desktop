# 第三方组件说明 / Third-party notice

`vendor/miot_kit/miot/` 下的代码来自小米官方 **miot-kit**（MIoT Python SDK），
版权归 **Xiaomi Corporation** 所有，遵循其 *Xiaomi Miloco License Agreement*。

本仓库内置该库**仅为方便 CI 构建出可独立运行的桌面程序**。其著作权与授权条款
归小米所有，使用须遵守小米的许可协议。如该协议不允许再分发，请将本目录改为在
构建时从合法来源获取，或将仓库设为私有。

> 注意：上游随库分发的摄像头 P2P 原生库（Linux 的 `.so` / macOS 的 `.dylib`）
> **未**包含在本目录中。

## `miot/libs/windows/x86_64/miot_camera_lite.dll`

上游只为 linux / darwin 构建摄像头 P2P 原生库，**从不分发 Windows 版本**——这也是
小米官方安装脚本（`scripts/install.ps1`）在 Windows 上直接要求改用 WSL 的唯一原因：
`MIoTClient.init_async()` 会构造 `MIoTCamera`，其 `__init__` 无条件加载该库，缺库即
在客户端初始化阶段抛 `FileNotFoundError`。

本目录下的 `miot_camera_lite.dll` 是**由本项目自行为 Windows x86_64 编译**的同一份
上游摄像头库（非小米官方分发的二进制），用于：

1. 让本桌面端在 Windows 上真正解码摄像头实时画面（而不是走 `server.py` 里的
   no-op 降级桩）；
2. 让 Miloco 能在 **Windows 裸机**（非 Docker/WSL）上运行——安装时由
   `backend/miloco_controller.py` 复制进 Miloco 自带的 `miot` 源码树再 pip 安装，
   见 README「关于 Miloco」。

其源码著作权同样归小米所有，分发与使用须遵守上述许可协议；若该协议不允许再分发
编译产物，请改为在本机自行编译后放入该路径（桌面端只按路径加载，不校验来源）。
