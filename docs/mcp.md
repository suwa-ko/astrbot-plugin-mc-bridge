# MCP Server 模式使用说明

从 `v2.0.0` 起，`mc_bridge` 核心引擎支持以标准 **Model Context Protocol (MCP)** 服务端暴露给任意 MCP 客户端（如 Claude Code、Antigravity、Cursor 等），与 AstrBot 插件共享同一套安全边界（A08-M 脱敏 `place_ref`、Protocol 4 推送、Coach Playbook）。

## 安装可选依赖

```bash
pip install "mcp>=1.2.0"
```

## 启动方式

```bash
python -m mc_bridge.mcp_server \
  --ws-host 127.0.0.1 \
  --ws-port 8766 \
  --token "YOUR_LONG_RANDOM_TOKEN" \
  --data-dir "./data" \
  --transport stdio
```

### MCP 客户端配置示例

```json
{
  "mcpServers": {
    "mc-bridge": {
      "command": "python",
      "args": [
        "-m", "mc_bridge.mcp_server",
        "--ws-host", "127.0.0.1",
        "--ws-port", "8766",
        "--token", "YOUR_LONG_RANDOM_TOKEN"
      ]
    }
  }
}
```

## 暴露的 Resources 与 Tools

- **MCP Resources**（只读、隐私脱敏）：
  - `mc://status`：客户端连接状态、协议版本、能力列表、血量/饱食/维度/群系
  - `mc://inventory`：背包摘要
  - `mc://memory/places`：结构化地点安全视图（仅 `place_ref`，无裸坐标）
  - `mc://tasks/active`：当前运行与排队任务
  - `mc://coach/playbook`：Coach 生效经验与代码问题计数
- **MCP Tools**（收敛后的核心高内聚工具面）：
  `mc_brief`、`mc_scan`、`mc_goto`、`mc_return_home`、`mc_stop`、`mc_plan`、`mc_task_start`、`mc_task_status`、`mc_task_cancel`、`mc_memory_find`、`mc_coach_digest`、`mc_coach_teach`。
