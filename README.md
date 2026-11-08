# AstrBot Minecraft Bridge

AstrBot 插件 + NeoForge 客户端 Mod，让 LLM Agent 通过真实 Minecraft 客户端执行生存任务：观察、移动、采集、合成、烧炼、容器搬运、战斗、睡觉和返回家床。

> 项目由两部分组成，必须保持版本兼容：`astrbot_plugin_mc_bridge/`（AstrBot 插件）和 `mod/`（NeoForge Mod）。

## 功能

- 通过 WebSocket 连接正在运行的 Minecraft 客户端，使用真实客户端状态和真实库存验收。
- L0 原语：状态、扫描、寻路、挖掘、放置、交互、容器、拾取、装备、截图等。
- L1/L2 技能：采集、合成、烧炼、批量容器搬运、建造、农业、睡觉、交易、战斗、探索、补给、返回家床。
- 纯 Python 任务调度器：优先级、依赖、租约、执行预算、抢占、取消、持久化任务指标和恢复快照。
- 完成度分离：`execution_state` 与 `goal_outcome` 分开记录，部分完成不会冒充整体完成。
- 安全边界：低血/敌人近身/目标不可达会停止；普通移动默认不拆方块，只有显式或回退策略才允许掘进。
- 可选共享记忆：只读取主人授权的 MC 相关私聊和游戏经历，不读取其他私聊。

## 架构

```text
QQ / WebChat / 游戏聊天
        │
        ▼
AstrBot + astrbot_plugin_mc_bridge
        │  WebSocket /ws (Bearer token)
        ▼
Minecraft NeoForge 客户端 + astrbot_bridge Mod
        │
        ├─ Baritone 寻路
        ├─ SurvivalActions 原语
        └─ 真实库存 / 方块 / 实体状态
```

插件不直接修改世界；所有动作都通过客户端 Mod 的受控原语执行，并在插件侧用真实状态验收。

## 环境要求

- AstrBot `>=4.16,<5`，推荐 Python 3.12。
- Minecraft Java Edition 1.21.1 + NeoForge 21.1.265。
- Baritone standalone 1.11.3（NeoForge 版本）。
- Python 依赖见 `astrbot_plugin_mc_bridge/requirements.txt`。
- Mod 构建需要 JDK 21。

## 安装插件

1. 把 `astrbot_plugin_mc_bridge/` 整个目录放到 AstrBot 的 `data/plugins/` 下。
2. 安装依赖：

   ```bash
   pip install -r astrbot_plugin_mc_bridge/requirements.txt
   ```

3. 重启 AstrBot，在插件配置中设置：
   - `token`：长随机字符串，必须与 Mod 配置一致；
   - `host` / `port` / `path`：WebSocket 监听地址；
   - `mc_provider_id`：MC Agent 使用的模型 Provider；
   - `owner_name` / `game_allowed_players`：主人游戏名；
   - `persona_id`：可选人格 ID。
4. 可参考根目录的 `config.example.json`。示例默认监听 `127.0.0.1`；如果 AstrBot 在 Docker 或需要远程客户端连接，改成 `0.0.0.0` 并限制端口/配置反向代理。

## 安装 Mod

1. 将 `astrbot_bridge-<version>.jar` 放入 Minecraft 的 `mods/`。
2. 安装 Baritone standalone 1.11.3 到同一 `mods/`。
3. 启动一次游戏，生成 `config/astrbot_bridge-common.toml`，设置：

   ```toml
   [bridge]
   url = "ws://127.0.0.1:8766/ws"
   token = "与插件一致的长随机字符串"
   autoConnect = true
   forwardChat = true
   ```

4. 进入世界后 Mod 会自动连接；连接成功后在 AstrBot 私聊发送 `mcstatus` 验证。

如果 AstrBot 运行在 Docker 或另一台机器，请把 `url` 改成可达的 `ws://` / `wss://` 地址，并配置端口映射或反向代理。

## 使用

QQ 私聊快捷命令：

```text
mcstatus    查看位置、血量、饥饿、家床和任务
mcstop      停止移动并清空任务
mcshot      截图
mchelp      帮助
mc <自然语言>  交给 MC Agent 规划和执行
```

游戏聊天中，使用 `game_chat_names` 中配置的名字呼唤机器人即可。

常用技能：

```text
mc采集64个原木
mc挖9个铁矿
mc把粗铁烧成铁锭
mc合成一把铁镐
mc把5个圆石和3个木棍放进箱子
mc回家床睡觉
mc去坐标 -100 64 200
```

详细命令和工具说明见 `docs/commands.md`。

## 配置与安全

- `token` 必须是长随机字符串，不能使用 `change-me`。
- 不要把真实 token、AstrBot 配置、插件数据目录、日志或任务指标提交到仓库。
- `/debug/status` 也需要 token；不要把 WebSocket 或调试端口直接暴露到公网。
- 推荐通过 TLS 反向代理暴露 `wss://`，并用防火墙限制来源。
- 公网部署前请阅读 `SECURITY.md`。

## 兼容性

| 插件 | Mod | 协议 |
|---|---|---|
| 1.1.x | 0.7.x | 3 |
| 1.0.x | 0.6.x | 3 |

协议细节见根目录 `protocol.md`。插件和 Mod 必须成对升级。

## 开发与测试

```bash
# Python 测试需要 AstrBot 运行时
pip install "astrbot==4.29.4" -r astrbot_plugin_mc_bridge/requirements.txt
MC_PLUGIN_FILE="$PWD/astrbot_plugin_mc_bridge/main.py" \
  python -m unittest discover -s tests -v

# Mod 构建与回归
cd mod
./gradlew assemble bridgeRegression navigationRegression survivalRegression
```

当前测试基线：Python 296 项；Java survival / navigation / bridge 回归。

## 已知限制

- 同一时间只执行一个主动角色任务；高优先级任务可以抢占低优先级可抢占任务。
- 任务恢复目前是“按快照参数重新执行”，还不是完整的阶段检查点续作。
- 部分技能可能返回 partial/unmet；依赖任务要求前置目标 fulfilled。
- 普通移动在确认无路时会先尝试开门，仍无路才回退一次受控掘进；建筑保护和破坏预算仍是后续工作。
- 工具面较大，Agent 轮次和上下文成本仍可继续优化。

## 许可证

MIT，见 `LICENSE`。Mod 构建模板中的 `mod/TEMPLATE_LICENSE.txt` 仅适用于 NeoForge MDK 模板文件。
