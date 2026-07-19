# Linking The World Temp Home Assistant Add-ons

这是 Linking The World 六恒科技系统的 Home Assistant 附加组件仓库。

安装 **Linking The World Temp Bridge** 后，附加组件会通过 MC7021 主机的本地
`yashcp` TCP/9000 协议接入六恒总控与房间子温控面板，并通过 MQTT Discovery
自动创建 Home Assistant 实体。

该附加组件不使用摩根云服务。请仅在已验证兼容的 MC7021 六恒系统中使用，
新住户应先以只读模式观察状态后再启用控制。
