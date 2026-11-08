# AstrBot Bridge (NeoForge Mod)

Minecraft NeoForge 1.21.1 客户端 Mod，通过 WebSocket 连接 AstrBot 插件，执行受控的生存动作。

## 构建

需要 JDK 21。

```bash
./gradlew assemble bridgeRegression navigationRegression survivalRegression
```

产物：

```text
build/libs/astrbot_bridge-<version>.jar
```

## 安装

1. 将 jar 放入 Minecraft `mods/`。
2. 安装 Baritone standalone 1.11.3（NeoForge）。
3. 启动一次，编辑 `config/astrbot_bridge-common.toml`：

   ```toml
   [bridge]
   url = "ws://127.0.0.1:8766/ws"
   token = "与 AstrBot 插件一致的长随机字符串"
   autoConnect = true
   forwardChat = true
   backgroundMode = true
   backgroundFps = 10
   autoRespawn = true
   ```

4. 进入世界后自动连接；在 AstrBot 私聊发送 `mcstatus` 验证。

如果 AstrBot 在 Docker/另一台机器，请改成可达的 `ws://` 或 `wss://` 地址。

## 能力

- 真实客户端状态：位置、维度、血量、饥饿、库存、附近实体、屏幕和路径反馈。
- L0 原语：扫描、寻路、挖掘、放置、容器、拾取、装备、交互、骑乘、交易、截图。
- 生存动作：合成、烧炼、批量容器搬运、种植、睡觉、梯子辅助。
- 安全：低血/着火/敌人/掉线停止；放置排除玩家自身；3×3 配方要求工作台。

## 兼容性

| Mod | 插件 | 协议 |
|---|---|---|
| 0.7.x | 1.1.x | 3 |
| 0.6.x | 1.0.x | 3 |

Mod 与插件必须成对升级。

## 许可

MIT。`TEMPLATE_LICENSE.txt` 仅适用于 NeoForge MDK 模板文件。
