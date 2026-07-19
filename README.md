# Linking The World Temp HA Bridge

面向 **Linking The World** 小区六恒科技系统的本地 Home Assistant
桥接项目。系统控制器来自 Moorgen，本项目通过 MC7021 已启用的本地
`yashcp` TCP/9000 通讯，将六恒总控和各房间子温控面板接入 Home Assistant。

整个过程只在局域网内运行：不依赖摩根云、云管理平台或 MT8157 模拟设备。

## 已支持功能

- 六恒科技系统总开关
- 制冷、制热、通风、除湿模式
- 居家/离家场景与冬季加湿
- 房间子温控面板的开启/关闭、整度设定温度、实际温度和湿度
- 子面板按主机实时上报自动发现，数量不限
- MQTT Discovery、Home Assistant Climate 卡片，以及 HomeKit Bridge 转发
- 主机状态确认、断线自动恢复，以及总控连接/最近命令/面板数量诊断实体

子面板的模式由六恒总控统一决定。例如总控处于制冷时，子面板卡片显示
“制冷/关闭”；选择“制冷”只开启该房间面板，不会改变总控模式。

## 适用范围

已验证范围是 MC7021 主机、LINKING THE WORLD 已交付的六恒总控虚拟设备
和房间温控面板。不同主机型号、未知固件或不同协议结构不应直接用于控制；
请先以只读模式观察状态上报。

## Home Assistant 附加组件安装

在 Home Assistant 的“设置 → 附加组件 → 附加组件商店”添加本仓库：

```text
https://github.com/jieinfo/ygsj-moorgen
```

安装 **Linking The World Temp Bridge**，填写主机局域网地址、本地主机账号和 MQTT
信息。默认使用 HA 的 Mosquitto 附加组件：`core-mosquitto:1883`。

首次为新住户配置时，建议：

1. 将 `allow_control` 设为 `false`。
2. 运行至少 24 小时，确认总控和所有子面板状态稳定上报。
3. 再将 `allow_control` 设为 `true`，并逐项验证控制效果。

## 生产验收

`0.2.0` 起，Bridge 只有收到与已配置总控 MAC 匹配的主机状态报文后才允许
控制。每个控制命令也必须在默认 8 秒内由主机状态回报确认；否则 HA 保留
上一次已确认的状态，并在 **科技系统总控** 设备的“最近控制命令”实体显示
确认超时。这样不会把网络发送成功误写成设备已执行。

正式投入使用前，建议完成以下检查：

1. 在 `allow_control: false` 下观察至少 24 小时，确认总控和全部房间面板出现并持续上报。
2. 逐项测试总控开关、四种模式、场景、冬季加湿和每个面板的开关/设定温度，确认“最近控制命令”均显示“已确认”。
3. 分别重启 MC7021、Mosquitto 和 Home Assistant，确认 Bridge 自动恢复并重新显示状态。
4. 断开并恢复局域网连接，确认“主机连接”会变为离线后自动恢复在线。
5. 与手机 App 并行操作一天，确认 App 操作会同步到 HA，且不出现反向误控。

安全项默认值：`require_protocol_verification: true`、`controller_silence_timeout: 90`、
`command_confirmation_timeout: 8`。不建议在生产环境关闭协议校验或缩短静默超时。

## 独立运行

```sh
cd moorgen_ha_bridge
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
cp config.example.yaml config.yaml
python3 bridge.py --config config.yaml --debug
```

每户若共用同一个 MQTT Broker，必须配置不同的 `mqtt.client_id` 和
`mqtt.topic_prefix`，避免状态串户或误控。请勿将主机 TCP/9000 或 MQTT
暴露到公网。

## 可靠性与诊断

- Mosquitto 短暂重启后，Bridge 会独立自动重连 MQTT 并重新发布设备状态。
- MC7021 读线程停止、TCP 断开或连续 90 秒无任何主机流量时，Bridge 会退出本轮
  会话；附加组件入口会在 15 秒后建立新会话。
- 子面板默认 900 秒未上报会在 HA 标记为不可用；可通过
  `thermostat_offline_after` 调整，设为 `0` 可关闭该检测。
- 原始主机状态报文发布在 `moorgen/tech_system/status_raw`，可用于排查
  未知设备或固件差异。

本项目是社区本地集成，不替代设备厂商的调试、保修或安全控制流程。
