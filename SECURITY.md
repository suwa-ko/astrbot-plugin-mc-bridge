# Security Policy

## 报告安全问题

请不要在公开 Issue 中粘贴真实 token、AstrBot 配置、QQ 号、服务器地址、日志或玩家数据。

请通过仓库维护者的私密渠道报告，或使用 GitHub Security Advisory。

## 部署安全

- `token` 必须是长随机字符串；不要使用默认 `change-me`。
- 插件和 Mod 的 token 必须一致，但不要通过公开仓库、截图或聊天记录传输。
- `/debug/status` 同样受 token 保护，但仍不应直接暴露到公网。
- WebSocket 推荐放在 TLS 反向代理后，使用 `wss://`，并用防火墙限制来源。
- 不要把 `data/plugins/`、`plugin_data/`、`observability.db`、`task_metrics.jsonl`、日志或截图目录提交到 Git。
- 如果 token 曾经进入公开提交、日志或备份，请立即在插件和 Mod 两侧同时轮换。

## 权限边界

插件只通过 Minecraft 客户端 Mod 的受控原语执行动作。客户端 Mod 拥有当前 Minecraft 账号的操作权限；请只在信任的账号和服务器上运行。

不要在公网服务器上使用拥有过高权限的 Minecraft 账号测试未经验证的配置。

## 记忆与隐私

- `memory.db`、`poi_memory.json`、`mc_shared_outbox.db` 只保存在本地数据目录，包含坐标或世界标识，不要提交到 Git、截图或公开日志。
- 模型上下文只接收 `place_ref` 和安全标签；精确坐标由主人私聊的本地卡片显示，不进入模型请求。
- LivingMemory、嵌入、重排和旧文档检索使用脱敏内容；旧坐标/私聊文档会标记为 `redacted` 并从语义检索中排除。
- 白名单群永久只读，只能看到主人显式分享的 `group_shared` 或本群专属地点，不能写入或控制游戏。
