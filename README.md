<p align="center">
  <img src="logo.png" alt="Minecraft 生存助手" width="160">
</p>

<h1 align="center">Minecraft 生存助手</h1>

<p align="center">
  让 AstrBot 通过真实 Minecraft 客户端观察世界、执行任务，并用实际结果回复你。
</p>

<p align="center">
  <a href="https://github.com/suwa-ko/astrbot-plugin-mc-bridge/actions/workflows/python-tests.yml"><img alt="Python Tests" src="https://github.com/suwa-ko/astrbot-plugin-mc-bridge/actions/workflows/python-tests.yml/badge.svg?style=flat-square"></a>
  <a href="https://github.com/suwa-ko/astrbot-plugin-mc-bridge/actions/workflows/mod-build.yml"><img alt="Mod Build" src="https://github.com/suwa-ko/astrbot-plugin-mc-bridge/actions/workflows/mod-build.yml/badge.svg?style=flat-square"></a>
  <a href="https://github.com/suwa-ko/astrbot-plugin-mc-bridge/releases"><img alt="Release" src="https://img.shields.io/github/v/release/suwa-ko/astrbot-plugin-mc-bridge?style=flat-square"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/suwa-ko/astrbot-plugin-mc-bridge?style=flat-square"></a>
</p>

## 这是什么

Minecraft 生存助手由两部分组成：

- **AstrBot 插件**：接收自然语言指令，规划并安排任务。
- **NeoForge 客户端 Mod**：在真实 Minecraft 客户端里执行动作，并返回真实状态。

它不是模拟器，也不会直接修改世界。所有采集、移动、合成和战斗都发生在你实际运行的 Minecraft 客户端中，任务结果以客户端返回的背包、方块和实体状态为准。

## 能做什么

| 场景 | 可以做什么 |
|---|---|
| 观察与导航 | 查看位置、血量、饥饿、背包、附近方块和实体；寻路到坐标、跟随玩家、返回家床 |
| 采集与生产 | 采集木材、石头、矿石、作物；合成工具和材料；烧炼物品 |
| 容器与整理 | 打开箱子/熔炉等容器，批量放入或取回物品，查看容器内容 |
| 建造与农业 | 建造直线、墙、地板、柱子和小屋；耕地、播种、收割、补种 |
| 生存与战斗 | 自动进食、睡觉、躲避危险、攻击附近敌对生物；低血时撤离 |
| 探索与补给 | 探索附近资源或地点，补充食物、火把、方块和工具 |
| 任务管理 | 自然语言提交任务，查看进度，取消任务；任务完成后主动通知 |
| 安全与保护 | 保护已登记的建筑和容器，限制单次/单任务破坏数量 |

### 任务示例

```text
mc采集64个原木
mc挖9个铁矿
mc把粗铁烧成铁锭
mc合成一把铁镐
mc把5个圆石和3个木棍放进箱子
mc回家床睡觉
mc去坐标 -100 64 200
mc探索附近有没有铁矿
```

## 快速开始

### 1. 环境要求

- AstrBot `>=4.16,<5`，推荐 Python 3.12
- Minecraft Java Edition 1.21.1 + NeoForge 21.1.265
- Baritone standalone 1.11.3（NeoForge 版本）
- JDK 21（仅构建 Mod 时需要）

### 2. 安装插件

从 [Releases](https://github.com/suwa-ko/astrbot-plugin-mc-bridge/releases) 下载 `astrbot_plugin_mc_bridge-<version>.zip`，在 AstrBot WebUI 的插件页选择“从文件安装”。

也可以直接从 Git 安装：仓库根目录就是插件目录，AstrBot 会自动读取 `metadata.yaml` 和 `main.py`。

安装后重启 AstrBot，在插件配置中至少设置：

- `token`：一段长随机字符串，必须和 Mod 配置一致
- `host` / `port` / `path`：插件监听地址
- `mc_provider_id`：执行 Minecraft 任务使用的模型 Provider
- `owner_name` / `game_allowed_players`：你的游戏名

### 3. 安装 Mod

1. 把 `astrbot_bridge-<version>.jar` 放入 Minecraft 的 `mods/`。
2. 把 Baritone standalone 1.11.3 放入同一个 `mods/`。
3. 启动一次游戏，编辑 `config/astrbot_bridge-common.toml`：

```toml
[bridge]
url = "ws://127.0.0.1:8766/ws"
token = "与插件一致的长随机字符串"
autoConnect = true
forwardChat = true
```

4. 进入世界后 Mod 会自动连接。在 AstrBot 私聊发送 `mcstatus` 验证。

如果 AstrBot 运行在 Docker 或另一台机器，请把 `url` 改成客户端可达的 `ws://` 或 `wss://` 地址。

### 4. 开始使用

QQ 私聊快捷命令：

```text
mcstatus    查看位置、血量、饥饿、家床和任务
mcstop      停止移动并清空任务
mcshot      截图
mchelp      帮助
mc <自然语言>  交给 Minecraft 助手规划和执行
```

游戏聊天中，在 `game_chat_names` 里配置机器人名字后，直接叫它即可。

## 配置说明

完整配置见插件配置页或根目录 `config.example.json`。常用项：

| 配置 | 作用 |
|---|---|
| `token` | 插件和 Mod 的连接令牌，必须一致 |
| `host` / `port` / `path` | WebSocket 监听地址，默认 `0.0.0.0:8766/ws` |
| `mc_provider_id` | 执行 Minecraft 任务使用的模型 |
| `owner_name` | 默认主人游戏名 |
| `game_allowed_players` | 允许通过游戏聊天指挥机器人的玩家 |
| `game_chat_names` | 游戏内唤醒机器人的名字 |
| `auto_survive` | 是否启用低血/危险自动反应 |
| `tool_profile` | `simple` 只暴露常用入口（默认）；`full` 暴露全部工具，适合调试 |
| `shared_memory_enabled` | 是否允许读取主人授权的 MC 相关聊天记忆 |

## 兼容性

| 插件 | Mod | 协议 |
|---|---|---|
| 1.3.x | 0.7.x | 3 |
| 1.2.x | 0.7.x | 3 |
| 1.1.x | 0.7.x | 3 |
| 1.0.x | 0.6.x | 3 |

插件和 Mod 必须成对升级。协议细节见 [`protocol.md`](protocol.md)。

## 安全

- `token` 必须是长随机字符串，不要使用默认值。
- 不要把真实 token、AstrBot 配置、日志、任务数据或截图提交到仓库。
- 不要把 WebSocket 或调试端口直接暴露到公网；推荐使用 TLS 反向代理和防火墙。
- 只在信任的 Minecraft 账号和服务器上运行。客户端 Mod 拥有该账号的正常操作权限。

更多说明见 [`SECURITY.md`](SECURITY.md)。

## 文档

- [命令与工具](docs/commands.md)
- [架构说明](docs/architecture.md)
- [开发与发布](docs/development.md)
- [连接协议](protocol.md)
- [更新日志](CHANGELOG.md)

## 开发

```bash
# Python 测试需要 AstrBot 运行时
pip install "astrbot==4.29.4" -r requirements.txt
MC_PLUGIN_FILE="$PWD/main.py" python -m unittest discover -s tests -v

# Mod 构建与回归
cd mod
./gradlew assemble bridgeRegression navigationRegression survivalRegression
```

当前基线：Python 324 项测试；Java survival / navigation / bridge 回归。

## 已知限制

- 同一时间只执行一个主动角色任务；高优先级任务可以抢占低优先级任务。
- 任务恢复对采集、合成、烧炼、搬运、建造和农业使用阶段检查点；移动、战斗等短技能仍以重新执行为主。
- 部分技能可能返回 partial/unmet；依赖任务要求前置目标达成。
- 普通移动默认不拆方块，无路时会先尝试开门，仍无路才回退一次受控掘进。
- 默认 `simple` 工具面只暴露常用入口；切换到 `full` 仍会暴露全部细粒度工具。
- 长任务完成后主动通知，但任务执行期间仍建议只在用户询问时查询一次进度。

## 许可证

MIT，见 [`LICENSE`](LICENSE)。`mod/TEMPLATE_LICENSE.txt` 仅适用于 NeoForge MDK 模板文件。
