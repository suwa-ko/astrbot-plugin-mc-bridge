# astrbot_plugin_mc_bridge

AstrBot 侧的 Minecraft 桥接插件。完整安装、配置和安全说明见仓库根目录 `README.md`。

## 安装

```bash
pip install -r requirements.txt
```

把本目录放到 AstrBot 的 `data/plugins/`，重启 AstrBot，在插件配置中设置：

- `token`：长随机字符串，必须与客户端 Mod 一致；
- `host` / `port` / `path`：WebSocket 监听；
- `mc_provider_id`：MC Agent 模型 Provider；
- `owner_name` / `game_allowed_players`：主人游戏名；
- `persona_id`：可选人格。

## 主要能力

- L0 客户端原语：状态、扫描、寻路、挖掘、放置、容器、拾取、装备、截图。
- L1/L2 技能：采集、合成、烧炼、批量搬运、建造、农业、睡觉、交易、战斗、探索、补给、返回。
- 任务调度：优先级、依赖、租约、预算、抢占、取消、恢复快照。
- 目标完成度：`fulfilled / partial / unmet / unknown`。

## 安全

- 不要提交真实 `token`、AstrBot 配置、日志、任务指标或截图。
- `/debug/status` 也需要 token，不要直接暴露公网。
- 公网部署请使用 TLS 反向代理和防火墙。

## 许可

MIT。
