# Linking The World Temp Home Assistant Add-ons

这是 Linking The World 六恒科技系统的 Home Assistant 附加组件仓库。

安装 **Linking The World Temp Bridge** 后，附加组件会通过 MC7021 主机的本地
`yashcp` TCP/9000 协议接入六恒总控与房间子温控面板，并通过 MQTT Discovery
自动创建 Home Assistant 实体。

该附加组件不使用摩根云服务。请仅在已验证兼容的 MC7021 六恒系统中使用，
新住户应先以只读模式观察状态后再启用控制。

`0.2.1` 起，控制命令必须由主机后续状态上报确认。总控设备还会提供“主机连接”、
“最近控制命令”和“已发现温控面板”诊断实体，便于验收与日常排障。正式启用控制前，
请依次验证总控、每个房间面板、主机重启、MQTT 重启和手机 App 并行操作。

`0.2.13` 起，每个房间温控面板还会提供“自动化温度”和“自动化湿度”。这两项是
经中位数与最小变化阈值处理的独立实体，适合用于自动化；原始温湿度仍保留给卡片显示和排障。
完整的防抖自动化 Blueprint 使用说明见仓库根目录 README。
