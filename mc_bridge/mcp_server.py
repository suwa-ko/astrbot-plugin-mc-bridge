"""MCP (Model Context Protocol) Server adapter for the Minecraft bridge core engine.

Exposes a converged set of 12 high-cohesion MCP Tools and 5 privacy-safe MCP
Resources over either ``stdio`` or ``streamable-http`` using the official Python
``mcp`` SDK (when installed), while keeping ``McBridgeMcpAdapter`` completely
dependency-free and unit-testable in any Python environment.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from .async_memory import AsyncMemoryStore
from .coach import CoachEngine, CoachStore
from .server import BridgeServer, parse_capabilities

MCP_RESOURCE_URIS = (
    "mc://status",
    "mc://inventory",
    "mc://memory/places",
    "mc://tasks/active",
    "mc://coach/playbook",
)

MCP_TOOL_NAMES = (
    "mc_brief",
    "mc_scan",
    "mc_goto",
    "mc_return_home",
    "mc_stop",
    "mc_plan",
    "mc_task_start",
    "mc_task_status",
    "mc_task_cancel",
    "mc_memory_find",
    "mc_coach_digest",
    "mc_coach_teach",
)


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


class McBridgeMcpAdapter:
    """Transport-agnostic MCP tool & resource dispatcher backed by mc_bridge core."""

    def __init__(
        self,
        *,
        bridge: BridgeServer,
        memory: AsyncMemoryStore | None = None,
        coach: CoachEngine | None = None,
        scheduler: Any = None,
        start_task_fn=None,
        start_plan_fn=None,
        cancel_task_fn=None,
    ):
        self.bridge = bridge
        self.memory = memory
        self.coach = coach
        self.scheduler = scheduler
        self._start_task_fn = start_task_fn
        self._start_plan_fn = start_plan_fn
        self._cancel_task_fn = cancel_task_fn

    # ------------------------------------------------------------ Resources

    async def read_resource(self, uri: str) -> str:
        key = str(uri or "").strip()
        if key == "mc://status":
            if not self.bridge.connected:
                return _dump({"connected": False, "protocol": 0, "capabilities": []})
            status = await self.bridge.call("status", timeout=10)
            safe = {
                "connected": True,
                "protocol": self.bridge.client_info.get("protocol", 3),
                "version": self.bridge.client_info.get("version", ""),
                "capabilities": sorted(parse_capabilities(self.bridge.client_info.get("capabilities"))),
                "player": status.get("player"),
                "dimension": status.get("dimension"),
                "biome": status.get("biome"),
                "health": status.get("health"),
                "food": status.get("food"),
                "survival_busy": status.get("survival_busy"),
            }
            return _dump(safe)

        if key == "mc://inventory":
            if not self.bridge.connected:
                return _dump({"connected": False, "items": []})
            inv = await self.bridge.call("inventory", {"operation": "view"}, timeout=10)
            return _dump(inv)

        if key == "mc://memory/places":
            if self.memory is None:
                return _dump({"places": []})
            rows = await self.memory.find_places(limit=20)
            # Never expose raw coordinates through MCP resources; emit place_ref safe views.
            safe_rows = [self.memory.sync.safe_view(r, audience="owner_private") for r in rows]
            return _dump({"places": safe_rows})

        if key == "mc://tasks/active":
            if self.scheduler is None:
                return _dump({"active": None, "pending": []})
            active = getattr(self.scheduler, "active", None)
            pending = self.scheduler.pending() if hasattr(self.scheduler, "pending") else []
            return _dump({
                "active": {"id": active.id, "kind": active.kind, "goal": active.goal, "state": active.state}
                if active is not None else None,
                "pending": [{"id": t.id, "kind": t.kind, "goal": t.goal, "priority": int(t.priority)} for t in pending],
            })

        if key == "mc://coach/playbook":
            if self.coach is None:
                return _dump({"enabled": False, "active_lessons": [], "open_code_issues": 0})
            active = await asyncio.to_thread(self.coach.store.list_lessons, status="active", limit=10)
            bugs = await asyncio.to_thread(self.coach.store.list_code_issues, status="open", limit=10)
            return _dump({
                "enabled": True,
                "active_lessons": [
                    {"id": r["id"], "scope": r["scope"], "condition": r["condition"], "rule": r["rule"]}
                    for r in active
                ],
                "open_code_issues": len(bugs),
            })

        raise ValueError(f"Unknown MCP resource URI: {key}")

    # ---------------------------------------------------------------- Tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        args = dict(arguments or {})
        if name == "mc_brief":
            return await self.read_resource("mc://status")

        if name == "mc_scan":
            radius = max(4, min(24, int(args.get("radius", 12) or 12)))
            data = await self.bridge.call("scan", {"radius": radius}, timeout=15)
            return _dump(data)

        if name == "mc_goto":
            x, y, z = int(args.get("x", 0)), int(args.get("y", 64)), int(args.get("z", 0))
            allow_break = bool(args.get("allow_break", False))
            data = await self.bridge.call("navigate", {"x": x, "y": y, "z": z, "allow_break": allow_break}, timeout=20)
            return _dump(data)

        if name == "mc_return_home":
            if self._start_task_fn is not None:
                res = await self._start_task_fn("return_home", {"reach": float(args.get("reach", 2.0))})
                return _dump({"started": res})
            return _dump({"error": "task runtime not attached"})

        if name == "mc_stop":
            data = await self.bridge.call("baritone", {"command": "stop"}, timeout=10)
            return _dump({"stopped": True, "client": data})

        if name == "mc_plan":
            steps = args.get("steps") or []
            if self._start_plan_fn is not None:
                res = await self._start_plan_fn(steps, goal=str(args.get("goal", "MCP 多步骤计划")))
                return _dump({"started": res})
            return _dump({"error": "plan runtime not attached"})

        if name == "mc_task_start":
            opcode = str(args.get("opcode") or "").strip()
            params = args.get("params") if isinstance(args.get("params"), dict) else {}
            if self._start_task_fn is not None:
                res = await self._start_task_fn(opcode, params)
                return _dump({"started": res})
            return _dump({"error": "task runtime not attached"})

        if name == "mc_task_status":
            return await self.read_resource("mc://tasks/active")

        if name == "mc_task_cancel":
            task_id = str(args.get("task_id") or "")
            if self._cancel_task_fn is not None:
                res = self._cancel_task_fn(task_id)
                return _dump(res)
            return _dump({"error": "cancel runtime not attached"})

        if name == "mc_memory_find":
            if self.memory is None:
                return _dump({"places": []})
            rows = await self.memory.find_places(query=str(args.get("query") or ""), limit=int(args.get("limit", 10) or 10))
            return _dump({"places": [self.memory.sync.safe_view(r, audience="owner_private") for r in rows]})

        if name == "mc_coach_digest":
            if self.coach is None:
                return "Coach 未启用"
            return await asyncio.to_thread(self.coach.format_digest)

        if name == "mc_coach_teach":
            if self.coach is None:
                return "Coach 未启用"
            scope = str(args.get("scope") or "global")
            rule = str(args.get("rule") or "")
            condition = str(args.get("condition") or "")
            lid = await asyncio.to_thread(self.coach.record_owner_feedback, scope, rule, condition)
            return _dump({"ok": bool(lid), "lesson_id": lid})

        raise ValueError(f"Unknown MCP tool: {name}")


def build_fastmcp_server(adapter: McBridgeMcpAdapter, *, name: str = "astrbot-mc-bridge"):
    """Mount ``McBridgeMcpAdapter`` onto the official ``mcp.server.fastmcp.FastMCP`` SDK."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "Official 'mcp' package is required to run the standalone MCP server. "
            "Install it with: pip install 'mcp>=1.2.0'"
        ) from exc

    mcp = FastMCP(name)

    @mcp.resource("mc://status")
    async def res_status() -> str:
        return await adapter.read_resource("mc://status")

    @mcp.resource("mc://inventory")
    async def res_inventory() -> str:
        return await adapter.read_resource("mc://inventory")

    @mcp.resource("mc://memory/places")
    async def res_places() -> str:
        return await adapter.read_resource("mc://memory/places")

    @mcp.resource("mc://tasks/active")
    async def res_tasks() -> str:
        return await adapter.read_resource("mc://tasks/active")

    @mcp.resource("mc://coach/playbook")
    async def res_playbook() -> str:
        return await adapter.read_resource("mc://coach/playbook")

    @mcp.tool()
    async def mc_brief() -> str:
        """Read current Minecraft client status, protocol, and capabilities."""
        return await adapter.call_tool("mc_brief")

    @mcp.tool()
    async def mc_scan(radius: int = 12) -> str:
        """Scan nearby blocks and entities around the Minecraft player."""
        return await adapter.call_tool("mc_scan", {"radius": radius})

    @mcp.tool()
    async def mc_goto(x: int, y: int, z: int, allow_break: bool = False) -> str:
        """Navigate the Minecraft client to coordinates."""
        return await adapter.call_tool("mc_goto", {"x": x, "y": y, "z": z, "allow_break": allow_break})

    @mcp.tool()
    async def mc_stop() -> str:
        """Stop client movement and clear active navigation."""
        return await adapter.call_tool("mc_stop")

    @mcp.tool()
    async def mc_memory_find(query: str = "", limit: int = 10) -> str:
        """Search remembered Minecraft locations (returns privacy-safe place_ref views)."""
        return await adapter.call_tool("mc_memory_find", {"query": query, "limit": limit})

    @mcp.tool()
    async def mc_coach_digest() -> str:
        """Read the Coach Playbook summary and open code issues."""
        return await adapter.call_tool("mc_coach_digest")

    @mcp.tool()
    async def mc_coach_teach(scope: str, rule: str, condition: str = "") -> str:
        """Teach an active Playbook rule directly to the Coach."""
        return await adapter.call_tool("mc_coach_teach", {"scope": scope, "rule": rule, "condition": condition})

    return mcp


def main() -> None:  # pragma: no cover - CLI entrypoint
    parser = argparse.ArgumentParser(description="Run standalone Minecraft Bridge MCP Server")
    parser.add_argument("--ws-host", default="127.0.0.1")
    parser.add_argument("--ws-port", type=int, default=8766)
    parser.add_argument("--ws-path", default="/ws")
    parser.add_argument("--token", required=True)
    parser.add_argument("--data-dir", default=".mc_bridge_data")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    args = parser.parse_args()

    from ..memory_store import MemoryStore

    data_dir = Path(args.data_dir)
    bridge = BridgeServer(args.ws_host, args.ws_port, args.ws_path, args.token)
    memory = AsyncMemoryStore(MemoryStore(data_dir / "memory.db"))
    coach = CoachEngine(CoachStore(data_dir / "coach.db"))
    adapter = McBridgeMcpAdapter(bridge=bridge, memory=memory, coach=coach)
    server = build_fastmcp_server(adapter)

    async def _serve():
        await bridge.start()
        try:
            if args.transport == "stdio":
                await server.run_stdio_async()
            else:
                await server.run_streamable_http_async()
        finally:
            await bridge.stop()
            memory.sync.close()
            coach.store.close()

    asyncio.run(_serve())


if __name__ == "__main__":  # pragma: no cover
    main()
