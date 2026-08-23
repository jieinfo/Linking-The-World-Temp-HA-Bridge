# Linking The World Temp HA changelog

## 0.1.1

- 修复 Home Assistant 2026.8 中配置向导无法加载并返回 500 的问题。
- 将配置表单改为前端可序列化的标准字段，并在提交后继续严格校验主机地址、客户端 ID 与总控 MAC。

## 0.1.0

- 新增可通过 HACS 安装的原生 Home Assistant 集成。
- 使用异步 TCP/9000 直接连接 MC7021，不依赖 MQTT 或 Mosquitto。
- 原生提供科技系统总开关、中文模式/场景、冬季加湿和房间 Climate 实体。
- 按主机状态上报动态发现任意数量的房间温控面板，并持久保存设备清单。
- 提供平滑温湿度、连接状态、协议验证、控制权限、最近命令和面板数量诊断实体。
- 保留原有 Linking The World Temp Bridge 附加组件，作为独立的兼容交付路径。
