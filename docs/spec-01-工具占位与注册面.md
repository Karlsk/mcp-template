# Spec 01：工具占位与注册面

> 状态：待实施
> 依据：`AGENTS.md`（= `CLAUDE.md`）§3 目录骨架、§5 调用规范、§8 新工具开发规范
> 范围：一次性钉死 6 个新工具的**注册面与参数契约**，实现留空
> 前置：无（本 spec 是 spec-02/03/04 的地基）

## Context

本轮要新增 6 个诊断类工具：

| 工具 | 数据源 | 定位 | 实现归属 |
|---|---|---|---|
| `get_fault_subgraph` | 图库拓扑快照 + 告警 | 生成故障子图（确定性为主，LLM 只提供设备/故障参数） | 后续 spec |
| `get_config_diff` | 控制器配置快照 + 图库 | 故障窗口内 `show run` diff（变更分析优先） | 后续 spec |
| `get_change_history` | PG 事件 | 故障窗口内变更事件 | 后续 spec |
| `get_topology_snapshot` | 图库 | 拓扑回放快照 | 后续 spec |
| `search_sop` | SOP 图库（Neo4j） | 受控检索：按故障类型/意图 | **spec-03** |
| `search_command_template` | 命令模板库（YAML） | 受控检索：按 intent × vendor | **spec-04** |

其中只有 `search_sop` 与 `search_command_template` 在本轮真正落实现。剩下 4 个的数据源
（PG 事件表、控制器配置快照）尚未接入，但它们的**工具名与参数契约现在就要定下来**——理由：

1. 注册面先定，spec-03/04 只替换函数体，不再动 `register_all` 与模块拆分，避免反复 merge；
2. 工具的 `name` + 参数签名是对模型的公开契约，早暴露早发现命名问题；
3. 占位工具返回明确的"已注册未实现"信封，比"工具不存在"对 agent 更友好——模型能知道
   这条路存在、只是暂不可用，不会反复尝试拼凑替代调用。

## 设计决策

1. **模块按域拆分**（遵循 §8.1「新工具 = 新文件 + `register_all` 一行」，便于后续 merge 上游）：

   | 新文件 | 工具 | 本轮状态 |
   |---|---|---|
   | `app/tools/sop_tools.py` | `search_sop` | 占位 → spec-03 实现 |
   | `app/tools/template_tools.py` | `search_command_template` | 占位 → spec-04 实现 |
   | `app/tools/graph_tools.py` | `get_fault_subgraph`、`get_topology_snapshot` | 占位 |
   | `app/tools/config_tools.py` | `get_config_diff` | 占位 |
   | `app/tools/change_tools.py` | `get_change_history` | 占位 |

   `get_fault_subgraph` 与 `get_topology_snapshot` 同属"图库拓扑"域，放同一模块；配置 diff 与
   变更历史各自独立成域（数据源不同：控制器配置快照 vs PG 事件）。

2. **第三种标准信封**：现有 `validation.py` 有 `skeleton_payload()`（未配置）与
   `unexpected_payload()`（兜底）。占位是第三种非成功语义，不能复用前两者——`skeleton_payload`
   会让模型以为"配好控制器就能用"，语义错误。新增 `not_implemented_payload()`。

3. **占位工具写完整签名**：函数体只做参数校验 + 返回占位信封，但 `@mcp.tool` 的 `name`/
   `description` 与参数列表**按最终契约写全**。这样模型看到的 schema 就是最终 schema，
   spec-03/04 及后续实现只替换函数体。

4. **两层 except 照抄**：即使占位实现不会抛业务异常，也保留 `except SDNError` / `except Exception`
   结构？——**不保留**。占位函数体不接触 client、不做 IO，加 except 只会产生不可达分支拖累
   覆盖率。等实现时按 §5 补齐两层兜底。参数校验失败仍返回 `{"ok": False, "detail": ...}`。

5. **占位仍做参数校验**：必填参数为空、时间窗非法等直接返回安全 detail。理由：这些校验规则
   属于工具契约的一部分，先落地能让 agent 侧的调用代码/提示词提前对齐。

## 分步实施

### 步骤 1：`app/tools/validation.py` 追加占位信封

在 `UNEXPECTED_DETAIL` 之后追加常量，在 `unexpected_payload()` 之后追加函数：

```python
NOT_IMPLEMENTED_DETAIL = "Tool is registered but not implemented yet."


def not_implemented_payload(hint: str | None = None) -> dict[str, object]:
    """Envelope returned by a registered-but-unimplemented tool.

    ``configured`` is False because no backing data source is wired yet. The
    optional ``hint`` names the pending data source so the agent can explain the
    gap instead of retrying.
    """
    payload: dict[str, object] = {
        "ok": False,
        "configured": False,
        "detail": NOT_IMPLEMENTED_DETAIL,
    }
    if hint:
        payload["hint"] = hint
    return payload
```

同时追加 spec-03 需要的正整数边界 helper（放在 `page_bounds_detail` 之后）：

```python
def positive_bound_detail(name: str, value: int, maximum: int) -> str | None:
    """Return a safe error detail if ``value`` is outside [1, maximum], else None."""
    if not 1 <= value <= maximum:
        return f"{name} must be in [1, {maximum}] (got {value})"
    return None
```

本轮结束后 `validation.py` 的完整清单（最后一项由 spec-03 追加，此处列出以便一眼看全信封族，
**本 spec 不实现它**）：

| 名称 | 语义 | 落地 spec |
|---|---|---|
| `SKELETON_DETAIL` / `skeleton_payload()` | SDN 控制器未配置 | 现有 |
| `UNEXPECTED_DETAIL` / `unexpected_payload()` | 兜底 | 现有 |
| `MAX_PAGE_SIZE` / `page_bounds_detail()` / `time_window_detail()` | 入参校验 | 现有 |
| `NOT_IMPLEMENTED_DETAIL` / `not_implemented_payload()` | 已注册未实现 | **spec-01** |
| `positive_bound_detail()` | 正整数区间校验 | **spec-01** |
| `GRAPH_SKELETON_DETAIL` / `graph_skeleton_payload()` | SOP 图库未配置 | spec-03 §6.4 |

### 步骤 2：`app/tools/graph_tools.py`（新文件）

```python
"""Graph-backed topology tools (placeholders).

``get_fault_subgraph`` and ``get_topology_snapshot`` will read the topology
snapshot graph (and alerts, for the fault subgraph). Neither data source is wired
yet, so both tools are registered with their final signature and return the
``not_implemented`` envelope. See docs/spec-01-工具占位与注册面.md.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from app.tools.validation import not_implemented_payload, time_window_detail


def register(mcp: FastMCP) -> None:
    """Register the graph topology tools."""

    @mcp.tool(
        name="get_fault_subgraph",
        description=(
            "Build the fault subgraph for a device/fault: the topology neighbourhood "
            "around the seed device plus the alerts raised inside the fault window. "
            "Deterministic by construction — the caller only supplies the device and "
            "fault parameters. NOT IMPLEMENTED YET: returns {ok: false, "
            "configured: false}."
        ),
    )
    async def get_fault_subgraph(
        device_name: str,
        fault_type: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        hops: int = 1,
    ) -> dict[str, object]:
        if not device_name.strip():
            return {"ok": False, "detail": "device_name must not be empty"}
        if detail := time_window_detail(start_time, end_time):
            return {"ok": False, "detail": detail}
        if not 1 <= hops <= 3:
            return {"ok": False, "detail": f"hops must be in [1, 3] (got {hops})"}
        return not_implemented_payload("topology snapshot graph + alerts")

    @mcp.tool(
        name="get_topology_snapshot",
        description=(
            "Replay the topology as it was at a point in time (graph snapshot). "
            "NOT IMPLEMENTED YET: returns {ok: false, configured: false}."
        ),
    )
    async def get_topology_snapshot(
        at_time: str | None = None,
        device_name: str | None = None,
    ) -> dict[str, object]:
        return not_implemented_payload("topology snapshot graph")
```

时间窗参数沿用现有控制器格式常量 `PERF_TIME_FORMAT`（`"%Y-%m-%d %H:%M:%S"`，见
`app/sdn/client.py`），复用 `time_window_detail` 校验，与已有性能/告警/日志工具一致。

### 步骤 3：`app/tools/config_tools.py`（新文件）

```python
"""Device configuration diff tool (placeholder).

``get_config_diff`` will diff two controller configuration snapshots taken inside
the fault window (change analysis first). The snapshot store is not wired yet.
"""
```

```python
    @mcp.tool(
        name="get_config_diff",
        description=(
            "Diff a device's running configuration between two points inside the "
            "fault window (change analysis first). NOT IMPLEMENTED YET: returns "
            "{ok: false, configured: false}."
        ),
    )
    async def get_config_diff(
        device_name: str,
        start_time: str | None = None,
        end_time: str | None = None,
        section: str | None = None,
    ) -> dict[str, object]:
        if not device_name.strip():
            return {"ok": False, "detail": "device_name must not be empty"}
        if detail := time_window_detail(start_time, end_time):
            return {"ok": False, "detail": detail}
        return not_implemented_payload("controller config snapshots + graph")
```

### 步骤 4：`app/tools/change_tools.py`（新文件）

```python
"""Change-event history tool (placeholder).

``get_change_history`` will read change events from PostgreSQL for the fault
window. The PG event source is not wired yet.
"""
```

```python
    @mcp.tool(
        name="get_change_history",
        description=(
            "List change events (config pushes, maintenance, upgrades) recorded "
            "inside the fault window, optionally scoped to one device. page_num is "
            "1-based. NOT IMPLEMENTED YET: returns {ok: false, configured: false}."
        ),
    )
    async def get_change_history(
        device_name: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        page_num: int = 1,
        page_size: int = 10,
    ) -> dict[str, object]:
        if detail := page_bounds_detail(page_num, page_size):
            return {"ok": False, "detail": detail}
        if detail := time_window_detail(start_time, end_time):
            return {"ok": False, "detail": detail}
        return not_implemented_payload("PostgreSQL change events")
```

### 步骤 5：`app/tools/sop_tools.py` 与 `app/tools/template_tools.py`（新文件，占位）

两者本轮先落占位，参数签名按 spec-03 / spec-04 的最终契约写：

```python
    # sop_tools.py
    async def search_sop(
        fault_type: str | None = None,
        intent: str | None = None,
        keyword: str | None = None,
        db: str | None = None,
        event_id: str | None = None,
        limit: int = 10,
        max_depth: int = 20,   # == graph.cypher.MAX_SOP_DEPTH (spec-03 §4.5)
    ) -> dict[str, object]:
        ...
        return not_implemented_payload("SOP graph (Neo4j)")
```

```python
    # template_tools.py
    async def search_command_template(
        action: str | None = None,
        vendor: str | None = None,
        keyword: str | None = None,
    ) -> dict[str, object]:
        return not_implemented_payload("command template library (YAML)")
```

> 注：spec-03 / spec-04 会把这两个函数体替换为真实实现（并按 §5 补 `ctx` 参数与两层
> except 兜底）。**签名中的参数名不得再改**，否则模型侧契约破裂。
>
> 占位阶段 `max_depth` 的默认值写字面量 `20`：`app/graph/` 还不存在（spec-02 才建），
> 此时无法 import `MAX_SOP_DEPTH`。spec-03 实现时把默认值改成 `MAX_SOP_DEPTH` 常量引用，
> **数值不变**，模型看到的 schema 因此保持一致。

### 步骤 6：`app/tools/__init__.py::register_all` 登记

```python
    from . import (
        alert_tools,
        change_tools,
        cmd_tools,
        config_tools,
        device_tools,
        graph_tools,
        link_tools,
        log_tools,
        perf_tools,
        sdn_tools,
        sop_tools,
        system,
        template_tools,
        topology_tools,
    )

    system.register(mcp)
    ...
    cmd_tools.register(mcp)
    sop_tools.register(mcp)
    template_tools.register(mcp)
    graph_tools.register(mcp)
    config_tools.register(mcp)
    change_tools.register(mcp)
```

保持"现有 9 行不动、新增 5 行追加在尾部"，import 列表按字母序（ruff `I` 规则要求）。

## 测试

新增 `tests/test_placeholder_tools.py`：

1. `list_tools` 包含 6 个新工具名（与既有 14 个工具共存，断言集合包含关系而非相等，避免
   后续加工具就红）。
2. 每个占位工具用合法参数调用一次 → 返回 `ok is False` 且 `detail == NOT_IMPLEMENTED_DETAIL`，
   `configured is False`，`hint` 非空（`get_topology_snapshot` 除外亦有 hint）。
3. 参数校验分支各命中一次：
   - `get_fault_subgraph(device_name="")` → detail 提示 device_name；
   - `get_fault_subgraph(device_name="R1", hops=9)` → detail 提示 hops 范围；
   - `get_config_diff(device_name="R1", start_time="bad")` → detail 提示时间格式；
   - `get_change_history(page_num=0)` → detail 提示 page_num。
4. `validation.py` 单元测试：`not_implemented_payload()` 无 hint 时不含 `hint` 键；
   `positive_bound_detail` 的三个边界（0 / 1 / maximum+1）。

复用现有 `make_session` fixture（占位工具不碰 SDN，任意 settings 均可）。

## 验收

- `uv run pytest` 全绿，覆盖率不低于 `--cov-fail-under=80`。
- `uv run ruff check .`、`uv run mypy` 干净。
- `uv run python scripts/test_client.py list-tools` 能看到 6 个新工具及其 description。
- **文档同步**：`AGENTS.md` 与 `CLAUDE.md`（两文件逐字节相同，必须同时改）
  - §3 目录骨架的 `app/tools/` 列表追加 5 个新模块及一句话说明；
  - §8.1 末尾补一句：占位工具的约定与 `not_implemented_payload` 的用途。
