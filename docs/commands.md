# 命令与工具

## QQ 私聊快捷命令

| 命令 | 说明 |
|---|---|
| `mcstatus` | 位置、血量、饥饿、家床和当前任务 |
| `mcstop` | 停止移动并清空任务 |
| `mcshot` | 截图 |
| `mchelp` | 帮助 |
| `mc <自然语言>` | 交给 MC Agent 规划和执行 |

默认 `qq_admin_only=true`，只有 AstrBot 管理员私聊可以控制机器人。

## 游戏聊天

在 `game_chat_names` 中配置机器人名字；游戏内说出名字即可对话。
游戏聊天路径有独立的冷却、重复过滤和身份校验。

## 常用自然语言

```text
mc看看现在的状态
mc去坐标 -100 64 200
mc采集64个原木
mc挖9个铁矿
mc把粗铁烧成铁锭
mc合成一把铁镐
mc把5个圆石和3个木棍放进箱子
mc把箱子里的圆石拿回来
mc回家床睡觉
mc探索附近有没有铁矿
```

## 技能工具

| 工具 | 作用 |
|---|---|
| `mc_gather` | 采集 logs/stone/coal/iron/wheat |
| `mc_craft` | 按服务器配方合成，自动准备材料/工作台 |
| `mc_smelt` | 找/放置熔炉，烧炼并核对产物 |
| `mc_container_transfer` | 批量放入/取回容器，只开关一次 |
| `mc_build` | 有界建造 line/wall/floor/pillar/box |
| `mc_farm` | 收割/补种小麦 |
| `mc_sleep` | 找床/回家床睡觉 |
| `mc_trade_for` | 村民交易 |
| `mc_combat` | 攻击敌对生物，低血撤离 |
| `mc_explore` | 有界探索资源/POI |
| `mc_resupply` | 补给食物、火把、方块和工具 |
| `mc_return_to` | 返回坐标/家床 |

## 查询工具

```text
mc_status / mc_brief      状态和任务摘要
mc_scan                   区域方块/实体扫描
mc_inspect_block          单方块详情
mc_block_context          方块上下文和可站位
mc_get_menu               当前菜单/容器
mc_task_status            任务状态和最近结果
mc_recipes                服务器同步配方
mc_protect                管理建筑/容器保护区域
```

## 动作原语

```text
mc_goto / mc_come / mc_follow / mc_stop
mc_mine_at / mc_dig_down / mc_place_at
mc_pickup / mc_container / mc_move_item
mc_use_item / mc_interact / mc_equip
```

具体参数以 AstrBot 工具列表为准。普通移动默认不开启破坏；被挡住时先尝试开门，仍无路才使用一次受控掘进。

默认 `tool_profile=simple` 暴露常用入口：状态、背包、方块上下文、扫描、移动/跟随/过来、挖掘/向下挖/放置/拾取、容器/交互、进食/装备/攻击、设置重生点、任务、配方、记忆、保护和截图。需要全部低层工具或调试时，可改为 `full`。
