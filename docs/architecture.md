# 架构说明

## 分层

```text
L2 目标技能   gather / craft / smelt / container_transfer / build / farm / ...
L1 原子技能   harvest_block / craft_recipe / smelt_batch / transfer_item / return_to / ...
L0 客户端原语 status / scan / navigate / mine_at / place_at / container / pickup / ...
```

- **L0**：客户端 Mod 执行的真实动作；插件只提交参数并核对客户端状态。
- **L1/L2**：Python 技能层组合 L0，负责找目标、接近、操作、验收和部分失败处理。
- **调度器**：优先级、依赖、资源租约、执行预算、抢占、取消和持久化记录。
- **Agent 层**：AstrBot LLM 选择工具和技能；默认 `simple` 工具面只暴露常用入口，`full` 用于调试。

## 仓库布局

- 仓库根目录：AstrBot 插件运行时代码、`metadata.yaml`、`_conf_schema.json`、`requirements.txt`。
- `mod/`：NeoForge 客户端 Mod。
- `tests/`：Python 回归。
- `docs/`：公开文档。

AstrBot 从 Git 安装和插件市场解析都要求仓库根目录存在 `metadata.yaml`，因此插件文件不能只放在子目录中。

## 任务生命周期

```text
入队
  → 等待资源/依赖
  → 获取租约
  → 设置执行预算
  → 执行（可取消/可抢占）
  → execution_state: completed / failed / cancelled
  → goal_outcome: fulfilled / partial / unmet / unknown
  → 指标、日志、恢复快照
```

`completed` 只表示执行过程结束，不表示目标已经达成。依赖任务默认要求前置目标 `fulfilled`。

## 检查点与恢复

- 任务开始时记录库存、位置、维度、血量和饥饿基线。
- 长时间技能在阶段边界写入检查点（已完成数量、已放置位置、已搬运物品等）。
- 任务组共享 `group_id` 和依赖关系；恢复时重新映射任务 ID，按依赖顺序重新入队。
- 恢复前校验世界和维度；背包与检查点不一致时给出警告，不伪装成未发生变化。
- 有安全断点的技能按剩余数量续作；没有安全断点的技能明确标记为 `rerun`。
- 恢复文件只保存有界摘要，不保存完整聊天、截图或背包原文。

## 破坏策略

- 显式挖掘、向下挖掘和导航回退共用同一份破坏策略。
- `dig_down` / `mine_below` 会逐层检查每格的方块类型和保护区域。
- 已登记建筑、容器和功能方块永远禁止破坏。
- 默认只允许自然方块类别（原木、石头、泥土、沙子、矿石、植物等）。
- 单次动作和单任务分别有破坏数量预算；超过预算直接拒绝。
- 建造完成后按已放置位置自动登记建筑区域，并记录维度；中途取消也会保护已完成部分。打开容器时自动登记容器保护区域；`mc_protect` 可手动管理。

## 安全边界

- 客户端 Mod 校验玩家、世界、连接代次和动作前置条件。
- 低血、着火、敌人近身、目标不可达、客户端掉线会停止。
- 普通移动默认不拆方块；无路时先尝试开门，仍无路则寻找附近可站立位置。只有显式 `allow_break=true` 才会受控掘进。
- 放置会排除玩家自身碰撞箱；3×3 配方会先要求工作台。
- 容器搬运一次可处理多种物品，但限制种类和总数，避免长时间占用。

## 数据

- `task_metrics.jsonl`：任务执行状态、耗时和目标完成度。
- `task_journal.jsonl`：调度器审计事件。
- `task_recovery.json`：显式中断恢复快照（含阶段检查点、库存基线和依赖分组）。
- `protected_regions.json`：已登记的建筑/容器保护区域。
- `observability.db`：模型轮次、工具调用和性能数据。
- 这些文件属于运行时数据，不应提交到仓库。

## 为什么是单仓库

插件和 Mod 通过协议 3 强耦合。单仓库能保证：

- 协议文档、插件能力过滤和 Mod capabilities 同步修改；
- 同一个 release 同时发布插件 zip 和 Mod jar；
- CI 同时跑 Python 和 Java 回归；
- 兼容性矩阵只有一个来源。
