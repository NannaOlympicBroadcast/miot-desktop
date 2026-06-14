# 第三方组件说明 / Third-party notice

`vendor/miot_kit/miot/` 下的代码来自小米官方 **miot-kit**（MIoT Python SDK），
版权归 **Xiaomi Corporation** 所有，遵循其 *Xiaomi Miloco License Agreement*。

本仓库内置该库**仅为方便 CI 构建出可独立运行的桌面程序**。其著作权与授权条款
归小米所有，使用须遵守小米的许可协议。如该协议不允许再分发，请将本目录改为在
构建时从合法来源获取，或将仓库设为私有。

> 注意：上游随库分发的摄像头 P2P 原生库（`miot/libs/` 下的 `.so` / `.dylib` /
> `.dll`）**未**包含在本目录中。Windows 平台本就缺少对应 DLL，桌面端会自动降级
> （摄像头可列出、可通过 SPEC 控制，但无法解码实时画面）。
