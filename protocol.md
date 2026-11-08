# Minecraft Bridge Protocol 3

AstrBot 插件与 NeoForge 客户端 Mod 之间的 WebSocket 协议。

## 连接

- 路径：`/ws`（可通过插件配置修改）
- 认证：`Authorization: Bearer <token>`，或查询参数 `?token=<token>`
- 传输：`ws://` 或 TLS 反向代理后的 `wss://`
- 单客户端：同一时间只接受一个 Minecraft 客户端连接

## 握手

客户端连接后发送 `hello`：

```json
{
  "type": "hello",
  "version": "0.7.0",
  "protocol": 3,
  "player": "PlayerName",
  "capabilities": ["status", "scan", "mine_at", "navigate", "operation", "..."]
}
```

插件根据 `protocol` 和 `capabilities` 过滤工具。协议 2 的旧能力会被拒绝或降级。

## 动作与结果

插件发送：

```json
{"type": "action", "id": "request-id", "action": "status", "params": {}}
```

客户端返回：

```json
{"type": "result", "id": "request-id", "ok": true, "data": {}}
```

失败：

```json
{"type": "result", "id": "request-id", "ok": false, "error": "reason"}
```

`id` 只用于请求/响应关联，插件侧还会校验连接代次，避免旧连接的结果污染新任务。

## 长动作

移动、挖掘、合成、烧炼、放置等可能返回 `operation_id`：

```json
{"operation_id": "op-uuid", "phase": "started"}
```

插件轮询：

```json
{"type": "action", "id": "...", "action": "operation", "params": {"operation_id": "op-uuid"}}
```

客户端返回 `phase=started/running/completed/failed`。只有 `completed + success=true` 才算成功；超时或取消会触发客户端 `stop`。

## 事件

客户端可主动发送：

```json
{"type": "event", "event": "chat", "data": {"kind": "player", "sender_name": "Player", "sender_uuid": "...", "body": "..."}}
```

用于游戏聊天、断线、重生等事件。插件会校验连接 id、玩家 UUID 和允许玩家列表。

## 状态与安全

- `status` 返回位置、维度、世界 key、血量、饥饿、任务占用、屏幕、附近敌对生物、路径反馈和库存摘要。
- `/debug/status` 是插件侧只读 HTTP 端点，同样需要 token；不要直接暴露公网。
- 客户端 Mod 不提供“伪造玩家聊天”或任意命令执行入口。
- 协议不传递真实 token 到动作参数中；token 只用于 WebSocket 握手。

## 兼容性

| 插件 | Mod | 协议 |
|---|---|---|
| 1.1.x | 0.7.x | 3 |
| 1.0.x | 0.6.x | 3 |

插件和 Mod 必须成对升级；不要混用不同协议的版本。
