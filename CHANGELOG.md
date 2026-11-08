# Changelog

## 1.1.0

- 新增批量容器搬运：`mc_container_transfer(items=...)` 一次搬多种物品，只打开/关闭容器一次。
- `mc_move_item` 的 `count` 可一次搬多个；新增批量搬运上限和参数校验。
- 修复任务取消：`mc_task_cancel` / `mc_stop` 统一停止后台任务和客户端动作，并确认 `survival_busy`。
- 引入 `goal_outcome`：区分执行完成和目标达成；partial/unmet 不再冒充 fulfilled。
- 任务依赖要求前置目标 fulfilled；任务指标增加 goal 统计。
- 技能预算/资源接入 TaskSpec；执行期间使用预算超时和租约续期。
- 新增高优先级抢占：低优先级可抢占任务会先停止并确认客户端空闲。
- 普通 goto 无路时先尝试开门，仍无路才回退一次受控掘进。
- 复合指令解析出未处理分句时，整句交给 Agent，不再静默丢步骤。
- 任务 ID 加入实例前缀，避免热重载后与恢复快照 ID 冲突。
- 恢复使用 remaining_seconds 收缩预算，并明确标记为 `rerun`。

## 0.7.0 (Mod)

- `scan` 新增 `doors` 数组，供插件优先尝试开门。
- `place_at` / `placementSite` 排除玩家自身碰撞箱，修复脚下放置超时。
- `beginCraft` 对 3×3 配方优先返回 `CRAFT_REQUIRES_TABLE`。
- 保留协议 3；与插件 1.1.x 配套。

## 0.6.0

- 首个公开整理版本的基础：L0 原语、L1/L2 技能、Baritone 安全寻路、真实库存验收、任务调度器。
- 协议 3；与插件 1.0.x 配套。
