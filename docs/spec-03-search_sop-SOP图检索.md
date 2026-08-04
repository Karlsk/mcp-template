# Spec 03：`search_sop` —— SOP 图受控检索

> 状态：待实施
> 依据：`AGENTS.md`（= `CLAUDE.md`）§5 调用规范、spec-02（`GraphClient` 与 `_db` 调用约定）
> 范围：SOP 图 schema 契约、`app/graph/models.py` 模型、`GraphClient` 三个业务方法、`app/tools/sop_tools.py::search_sop`
> 前置：spec-01（占位已注册）、spec-02（`Neo4jClient` / `GraphClient` 骨架就绪）

## Context

`search_sop` 是排障链路的入口：agent 拿到告警/故障描述后，用故障类型或意图检索出对应的
**标准处置流程（SOP）**，然后按流程逐步执行——每一步的 `action` 再喂给
`search_command_template`（spec-04）换成具体厂商命令。

"受控检索"的含义：检索的**范围与结果结构由服务端确定**，LLM 只提供故障类型/意图这类参数，
不参与图遍历、不拼 Cypher、不决定返回哪些节点。

SOP 在图库中的组织形态（用户给定）：

- 一个 **Event 是一个 SOP 钩子点，也是一棵树的根**；从 Event 出发到全部 Output 构成一棵完整
  的 SOP 树。
- **一个逻辑库（`_db`）里可以有多个 Event**。逻辑库是一组 SOP 的容器，`_db` 保证**展树时不会
  串到别的逻辑库**——这是"避免图混乱"的机制。
- 因此 **`(db, event_id)` 才唯一定位一棵树**，单靠 `event_id` 不保证唯一。

## 1. Schema 契约

```
(:Event  {id, name, fault_type, intent, aliases: [str], description, _db})
(:Step   {id, name, action, observation, _db})
(:Output {id, name, answer, _db})

(:Event)-[:NEXT {condition}]->(:Step)
(:Step) -[:NEXT {condition}]->(:Step | :Output)
```

字段语义：

| 节点 | 字段 | 含义 |
|---|---|---|
| Event | `name` | 人读的钩子点名称 |
| Event | `fault_type` | 故障类型（受控检索的主键之一） |
| Event | `intent` | 意图（受控检索的主键之一） |
| Event | `aliases` | 同义写法列表，精确匹配时一并参与 |
| Step | `name` | 人读步骤名，如"检查接口状态" |
| Step | `action` | **机读意图**，如 `verify_interface_state`；spec-04 命令模板库的一级 key |
| Step | `observation` | 需要从命令回显里提取的字段名，如 `oper_state` |
| Output | `answer` | final answer 文本 |

边的 `condition`：

- **分支边**才有，形如 `oper_state=up`（对应上游 Step 的 `observation` 取值）；
- **直连边**没有 `condition`（属性缺失，不是空串）——模型侧用 `None` 表示，工具输出保留
  `null`，让 agent 能区分"无条件直连"与"条件为空"。

标签名、关系类型、`_db` 属性名全部集中在 `app/graph/cypher.py` 常量块（spec-02 §10）。
真实图库若用了别的命名（如 `:SOPEvent` / `:FLOW`），**只改这一个文件**。

## 2. 两阶段 db 语义（本 spec 的核心）

```
阶段 1  发现（跨库）              阶段 2  展树（锁库）
────────────────────             ────────────────────
find_sop_events(...)             get_sop_tree(db, event_id, ...)
db_tag=None                      db_tag=db
allow_cross_db=True              （$_db 断言生效）
查 (:Event)，回显 e._db AS db     遍历路径上每个节点都必须 _db = $_db
```

1. **发现阶段必须跨库**：agent 只知道"故障类型是链路中断"，不知道该 SOP 存在哪个逻辑库里，
   所以候选检索要扫全部 Event。这是全仓库唯一需要 `allow_cross_db=True` 的地方
   （grep `allow_cross_db` 即可审计）。**每个候选回显它自己的 `db`**。
2. **展树阶段必须锁库**：`get_sop_tree` 同时需要 `db` 与 `event_id`，`db_tag=db` 下传，
   且**遍历路径上的每个中间节点**都要满足 `_db = $_db`。这样即使两个逻辑库里存在同 id 的
   节点、或存在跨库的脏边，也不可能把两棵树并成一棵。

可选收窄：`find_sop_events` 接受 `db=None` 参数，给定时把发现范围限制到单库（此时走正常的
`db_tag=db` 路径，不需要 `allow_cross_db`）。

## 3. Models（`app/graph/models.py`）

纯数据、不 import 内部模块、无 IO（§4）。图库 schema 会演进，一律 `extra="allow"`
保留未知字段（"瘦类型规则"：只给 JSON 类型确定的字符串字段和列表加类型）。

```python
class SOPNode(BaseModel):
    """A node in a SOP tree (Event / Step / Output share this shape)."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    kind: str = ""            # "event" | "step" | "output" — derived from the label
    name: str = ""
    action: str = ""          # Step only: the command-template intent key
    observation: str = ""     # Step only: field to extract from the command output
    answer: str = ""          # Output only


class SOPEdge(BaseModel):
    """A :NEXT edge. ``condition`` is None for a plain (unconditional) edge."""

    model_config = ConfigDict(extra="allow")

    source: str = ""
    target: str = ""
    condition: str | None = None


class SOPCandidate(BaseModel):
    """One matched Event. ``db`` + ``event_id`` together locate its tree."""

    model_config = ConfigDict(extra="allow")

    db: str = ""
    event_id: str = ""
    name: str = ""
    fault_type: str = ""
    intent: str = ""


class SOPTree(BaseModel):
    """A complete SOP tree: one Event root, every reachable Step/Output, all edges."""

    model_config = ConfigDict(extra="allow")

    db: str = ""
    event: SOPNode
    nodes: list[SOPNode] = Field(default_factory=list)
    edges: list[SOPEdge] = Field(default_factory=list)
    truncated: bool = False
```

`kind` 字段由 client 从节点 label 推导（`labels(n)` 回显），而不是让 agent 靠字段有无去猜
节点类型。三类节点共用一个 `SOPNode` 而不是三个模型：树里节点是混排的，一个列表 + `kind`
判别比三个列表更好用，且新增节点类型时不破坏结构。

`SOPTree` 与 spec-02 §11 的 `GraphFragment` 形状同源（`nodes` + `edges` + `truncated`），
保持"序列化中立"以便将来接 networkx 适配器。

## 4. Cypher（`app/graph/cypher.py`）

### 4.1 精确匹配（发现阶段第一段）

```python
FIND_EVENTS_EXACT = f"""
MATCH (e:{LABEL_EVENT})
WHERE ($_db IS NULL OR e.{DB_PROPERTY} = $_db)
  AND (
    ($fault_type IS NOT NULL AND toLower(e.fault_type) = $fault_type)
    OR ($intent IS NOT NULL AND toLower(e.intent) = $intent)
    OR ($needle IS NOT NULL AND toLower(e.name) = $needle)
    OR ($needle IS NOT NULL AND $needle IN [a IN coalesce(e.aliases, []) | toLower(a)])
  )
RETURN e.{DB_PROPERTY} AS db, e.id AS event_id, e.name AS name,
       coalesce(e.fault_type, '') AS fault_type, coalesce(e.intent, '') AS intent
ORDER BY db, event_id
LIMIT $limit
"""
```

- 所有入参在 client 侧先 `.strip().lower()`，Cypher 里只与 `toLower(...)` 比较，
  大小写不敏感由**两侧同时归一**保证。
- `$needle` 是"名字类"匹配项：`keyword` 优先，没给则退回 `fault_type or intent`
  （用户常把故障类型直接写成 SOP 名）。
- `WHERE ($_db IS NULL OR ...)`：**注意**跨库调用时 `db_tag=None`，spec-02 的
  `run_read` 不会注入 `_db` 参数，而 Cypher 里引用了未传的参数会被 Neo4j 拒绝
  （`ParameterMissing`）。因此 client 在跨库调用时必须**显式传 `{"_db": None}`**，让参数存在
  但为 null。这条要写成注释钉在 client 里，并有测试覆盖。

  这也意味着 `FIND_EVENTS_*` 文本里含 `$_db`，即使 `db_tag=None` 走 `allow_cross_db=True`
  路径也无害（`_check_db_scope` 只在 `db_tag` 非 None 时要求含 `$_db`）。同一条语句因此能
  同时服务"跨库发现"和"单库收窄"两种调用，不必写两份。

### 4.2 模糊降级（发现阶段第二段，仅零命中时执行）

```python
FIND_EVENTS_FUZZY = f"""
MATCH (e:{LABEL_EVENT})
WHERE ($_db IS NULL OR e.{DB_PROPERTY} = $_db)
  AND $needle IS NOT NULL
  AND (
    toLower(coalesce(e.name, '')) CONTAINS $needle
    OR toLower(coalesce(e.fault_type, '')) CONTAINS $needle
    OR toLower(coalesce(e.intent, '')) CONTAINS $needle
    OR toLower(coalesce(e.description, '')) CONTAINS $needle
    OR ANY(a IN coalesce(e.aliases, []) WHERE toLower(a) CONTAINS $needle)
  )
RETURN e.{DB_PROPERTY} AS db, e.id AS event_id, e.name AS name,
       coalesce(e.fault_type, '') AS fault_type, coalesce(e.intent, '') AS intent
ORDER BY db, event_id
LIMIT $limit
"""
```

**两段查询分开写、不合成一条**：精确与模糊的语义完全不同（前者是"就是它"，后者是
"可能是它"），合并后既难解释也难测。分开还能让工具输出标注 `match` 为 `"exact"` /
`"fuzzy"`，让 agent 知道结果的置信度。

### 4.3 展树（锁库，两步纯 Cypher，不依赖 APOC）

**第一步：收集可达节点。**

```python
def sop_tree_nodes(max_depth: int) -> str:
    """Build the reachable-nodes query. ``max_depth`` MUST be a validated int:
    Cypher does not allow a parameterized variable-length upper bound, so it is
    interpolated into the statement text (see the injection note below).
    """
    return f"""
MATCH (e:{LABEL_EVENT})
WHERE e.{DB_PROPERTY} = $_db AND e.id = $event_id
OPTIONAL MATCH path = (e)-[:{REL_NEXT}*1..{max_depth}]->(m)
WHERE ALL(n IN nodes(path) WHERE n.{DB_PROPERTY} = $_db)
WITH e, collect(DISTINCT m) AS reached
UNWIND ([e] + reached) AS n
WITH DISTINCT n WHERE n IS NOT NULL
RETURN n.id AS id, labels(n) AS labels, n.name AS name,
       coalesce(n.action, '') AS action,
       coalesce(n.observation, '') AS observation,
       coalesce(n.answer, '') AS answer,
       properties(n) AS props
LIMIT $node_limit
"""
```

要点：

- **`ALL(n IN nodes(path) WHERE n._db = $_db)` 是必需的**：变长模式无法在模式内部过滤中间
  节点，只写 `e._db = $_db` 只锁住了根，遍历仍可能穿到别的逻辑库。这是本 spec 最容易写错
  的一行。
- `OPTIONAL MATCH`：单节点 SOP（Event 直接就是叶子，尚未编排步骤）也要能返回，不能因无
  出边就整棵树查空。
- `properties(n)` 一并回显，配合模型的 `extra="allow"` 保留图库演进出的新字段。
- `LIMIT $node_limit`：`MAX_SOP_NODES + 1`，多取一个用来判断是否发生截断。

**第二步：收集节点集合内的边。**

```python
SOP_TREE_EDGES = f"""
MATCH (a)-[r:{REL_NEXT}]->(b)
WHERE a.{DB_PROPERTY} = $_db AND b.{DB_PROPERTY} = $_db
  AND a.id IN $node_ids AND b.id IN $node_ids
RETURN a.id AS source, b.id AS target, r.condition AS condition
ORDER BY source, target
"""
```

在已确定的节点集合内取边，天然包含所有分支边与汇聚边，且两端都带 `_db` 过滤。
`r.condition` 属性缺失时 Neo4j 返回 `null` → 模型的 `condition: str | None` 收到 `None`，
正好表达"无条件直连"。

**为什么不用一条 `MATCH path = ...` 直接返回 path**：一棵有分支的 SOP 树里，同一个下游
节点会出现在多条 path 上，返回 path 会产生 O(路径数) 的重复数据（分支多时指数级膨胀），
而节点集 + 边集是 O(V + E)。两步查询也让"截断"判断变简单。

### 4.4 变长上界内联的安全性论证

Neo4j **不允许参数化变长上界**：`[:NEXT*1..$max_depth]` 是语法错误。因此必须把整数拼进
语句文本。安全性由三重保证：

1. 工具层用 `validation.positive_bound_detail("max_depth", max_depth, MAX_SOP_DEPTH)` 校验，
   越界直接返回错误、不进 client；
2. 参数在 Python 侧类型为 `int`（FastMCP 依 schema 做类型转换，非整数值在 MCP 层就被拒），
   client 内再 `int(max_depth)` 兜底；
3. 拼接点只有 `sop_tree_nodes()` 一个函数，且只拼这一个整数——没有任何用户**字符串**进入
   Cypher 文本（标签名来自本模块常量，其余全走 `$params`）。

把这三条写成 `sop_tree_nodes()` 的 docstring，并用测试钉死"传入非法 max_depth 时工具在校验
层就返回错误"。

### 4.5 常量

```python
MAX_SOP_DEPTH: Final = 20      # variable-length traversal cap (cycle guard)
MAX_SOP_NODES: Final = 200     # node budget per tree
MAX_SOP_CANDIDATES: Final = 50 # upper bound for the `limit` tool parameter
```

环形图安全：SOP 树理论上无环，但脏数据可能造出环。深度上限 + 节点预算 + `DISTINCT` 三者
共同保证查询有界；再叠加 `Neo4jClientConfig.query_timeout` 作最后防线。命中任一上限时
`truncated=True`，让 agent 知道流程可能不完整。

## 5. `GraphClient` 业务方法

追加到 `app/graph/client.py` 类尾（spec-02 已给出统一形状）。

```python
async def find_sop_events(
    self,
    *,
    fault_type: str | None = None,
    intent: str | None = None,
    keyword: str | None = None,
    db: str | None = None,
    limit: int = 10,
) -> tuple[list[SOPCandidate], str]:
    """Find candidate SOP Events. Returns (candidates, match_mode).

    ``match_mode`` is "exact", "fuzzy", or "none". Discovery spans every logical
    database unless ``db`` narrows it — the only cross-db query in the codebase.
    """
```

实现要点：

```python
    self._require_configured()
    needle = _normalize(keyword) or _normalize(fault_type) or _normalize(intent)
    params = {
        # Cypher references $_db even on the cross-db path, so the parameter must
        # exist (as null) or Neo4j raises ParameterMissing.
        "_db": None,
        "fault_type": _normalize(fault_type),
        "intent": _normalize(intent),
        "needle": needle,
        "limit": limit,
    }
    cross = db is None
    rows = await self._run(FIND_EVENTS_EXACT, params, db_tag=db, allow_cross_db=cross,
                           query_name="find_sop_events_exact")
    mode = "exact"
    if not rows and needle:
        rows = await self._run(FIND_EVENTS_FUZZY, params, db_tag=db, allow_cross_db=cross,
                               query_name="find_sop_events_fuzzy")
        mode = "fuzzy"
    if not rows:
        mode = "none"
    return [SOPCandidate.model_validate(row) for row in rows], mode
```

注意 `db` 非 None 时 `params["_db"]` 会被 `run_read` 覆盖为 `db`（spec-02 的注入发生在
合并后的 dict 上），所以这里预置 `None` 只服务跨库路径。这个覆盖顺序要在 `run_read` 里
写清（`merged["_db"] = db_tag` 在 `dict(params)` 之后）。

```python
async def resolve_sop_event(self, event_id: str) -> list[SOPCandidate]:
    """Reverse-lookup an Event by id across logical databases.

    Used when the caller supplies ``event_id`` without ``db``. Ids are not
    guaranteed globally unique, so multiple hits are returned rather than guessed.
    """
```

```python
async def get_sop_tree(
    self, *, db: str, event_id: str, max_depth: int = MAX_SOP_DEPTH
) -> SOPTree | None:
    """Fetch one complete SOP tree, scoped to a single logical database.

    Returns None when no Event matches ``(db, event_id)``. Traversal is confined
    to ``db``: every node on every path must carry the same ``_db``.
    """
```

实现要点：

```python
    self._require_configured()
    depth = max(1, min(int(max_depth), MAX_SOP_DEPTH))
    rows = await self._run(
        sop_tree_nodes(depth),
        {"event_id": event_id, "node_limit": MAX_SOP_NODES + 1},
        db_tag=db,
        query_name="sop_tree_nodes",
    )
    if not rows:
        return None
    truncated = len(rows) > MAX_SOP_NODES
    rows = rows[:MAX_SOP_NODES]
    nodes = [_to_node(row) for row in rows]
    edge_rows = await self._run(
        SOP_TREE_EDGES,
        {"node_ids": [n.id for n in nodes]},
        db_tag=db,
        query_name="sop_tree_edges",
    )
    event = next((n for n in nodes if n.kind == "event"), None)
    if event is None:
        raise GraphQueryError(
            "SOP graph response was malformed.",
            detail=f"no Event node in tree rows for {db}/{event_id}",
        )
    return SOPTree(
        db=db,
        event=event,
        nodes=nodes,
        edges=[SOPEdge.model_validate(r) for r in edge_rows],
        truncated=truncated,
    )
```

`_to_node` 把 `labels` 映射为 `kind`（`Event`→`event`、`Step`→`step`、`Output`→`output`，
未知 label 保留原样小写），并把 `props` 里的额外字段合并进模型（走 `extra="allow"`）。

`_run` 是私有薄封装：调 `self._neo4j.run_read(...)`，`except Exception as exc: raise
_map_neo4j_error(exc) from exc`，避免每个业务方法重复写 try/except（**唯一**处理驱动异常的
地方仍是 `client.py`，符合 §4）。

## 6. Tool（`app/tools/sop_tools.py`）

替换 spec-01 的占位函数体。参数名与 spec-01 一致（契约不变）。

```python
def register(mcp: FastMCP) -> None:
    """Register the SOP graph search tool."""

    @mcp.tool(
        name="search_sop",
        description=(
            "Search the SOP graph for the standard operating procedure matching a "
            "fault type or intent, and return its full decision tree. Matching is "
            "exact-first (name/alias/fault_type/intent), falling back to keyword "
            "substring. One hit returns the tree (mode='tree'); several hits return "
            "candidates (mode='candidates') — re-call with the candidate's db AND "
            "event_id to expand one. Each step carries an 'action' key: feed it to "
            "search_command_template to get the vendor-specific command. Returns "
            "{ok, configured, mode, match, db, event, nodes[], edges[], truncated}."
        ),
    )
    async def search_sop(
        ctx: Context[Any, Any, Any],
        fault_type: str | None = None,
        intent: str | None = None,
        keyword: str | None = None,
        db: str | None = None,
        event_id: str | None = None,
        limit: int = 10,
        max_depth: int = MAX_SOP_DEPTH,
    ) -> dict[str, object]:
```

### 6.1 参数校验（返回安全 detail，不抛）

```python
        if not any((fault_type, intent, keyword, event_id)):
            return {
                "ok": False,
                "detail": "provide at least one of fault_type / intent / keyword / event_id",
            }
        if detail := positive_bound_detail("limit", limit, MAX_SOP_CANDIDATES):
            return {"ok": False, "detail": detail}
        if detail := positive_bound_detail("max_depth", max_depth, MAX_SOP_DEPTH):
            return {"ok": False, "detail": detail}
```

### 6.2 分发逻辑

```python
        try:
            graph: GraphClient = ctx.request_context.lifespan_context["graph_client"]
            if not graph.configured:
                return graph_skeleton_payload()

            # Direct expansion: (db, event_id) locates exactly one tree.
            if event_id and db:
                return _tree_payload(await graph.get_sop_tree(db=db, event_id=event_id,
                                                              max_depth=max_depth), "exact")

            # event_id without db: reverse-lookup, expand only when unambiguous.
            if event_id:
                found = await graph.resolve_sop_event(event_id)
                if len(found) == 1:
                    return _tree_payload(
                        await graph.get_sop_tree(db=found[0].db, event_id=event_id,
                                                 max_depth=max_depth), "exact")
                if not found:
                    return _empty_payload("exact")
                return _candidates_payload(found, "exact")

            candidates, match = await graph.find_sop_events(
                fault_type=fault_type, intent=intent, keyword=keyword, db=db, limit=limit
            )
            if not candidates:
                return _empty_payload(match)
            if len(candidates) == 1:
                only = candidates[0]
                return _tree_payload(
                    await graph.get_sop_tree(db=only.db, event_id=only.event_id,
                                             max_depth=max_depth), match)
            return _candidates_payload(candidates, match)
        except GraphError as exc:
            await ctx.error(f"search_sop failed: {exc}")
            return {"ok": False, "configured": True, "detail": str(exc)}
        except Exception:
            logger.exception("search_sop unexpected failure")
            return unexpected_payload()
```

只给 `db` 不给其它 → 落到 `find_sop_events(db=db)`，即"在这个逻辑库里按其它条件找"。
若 `db` 是唯一参数，前面的"入参全空"校验已把它挡住（`db` 不算检索条件），返回提示。
—— 按上面的 `any((fault_type, intent, keyword, event_id))` 写法，只给 `db` 会被拒，这是
故意的：`db` 是**范围限定符**，不是检索条件。

### 6.3 输出信封

三个 helper（模块私有）：

```python
def _tree_payload(tree: SOPTree | None, match: str) -> dict[str, object]:
    if tree is None:
        return {"ok": False, "configured": True,
                "detail": "no SOP event matches the given db and event_id"}
    return {
        "ok": True,
        "configured": True,
        "mode": "tree",
        "match": match,
        "db": tree.db,
        "event": tree.event.model_dump(),
        "nodes": [n.model_dump() for n in tree.nodes],
        "edges": [e.model_dump() for e in tree.edges],
        "truncated": tree.truncated,
        "next_step_hint": (
            "For each node with kind='step', call "
            "search_command_template(action=<step.action>, vendor=<device vendor>)."
        ),
    }


def _candidates_payload(candidates: list[SOPCandidate], match: str) -> dict[str, object]:
    return {
        "ok": True,
        "configured": True,
        "mode": "candidates",
        "match": match,
        "candidates": [c.model_dump() for c in candidates],
        "next_step_hint": "Re-call search_sop with both db and event_id of one candidate.",
    }


def _empty_payload(match: str) -> dict[str, object]:
    return {
        "ok": True,
        "configured": True,
        "mode": "empty",
        "match": match,
        "candidates": [],
        "detail": "no SOP event matched; try a broader keyword or a different fault_type",
    }
```

零命中是 `ok: True`（查询成功、结果为空），不是 `ok: False`（调用失败）。这个区分对 agent
很重要：前者应改关键词重试或走通用排障，后者应报告故障。

`event` 既单列又包含在 `nodes` 里（`kind == "event"`）：单列让 agent 一眼看到根，
`nodes` 保持完整以便结构化遍历。冗余是刻意的，不去掉。

### 6.4 骨架模式信封

`app/tools/validation.py` 追加图库版骨架信封（SDN 版文案不适用）：

```python
GRAPH_SKELETON_DETAIL = "SOP graph not configured (skeleton mode)."


def graph_skeleton_payload() -> dict[str, object]:
    """Envelope returned when the SOP graph is not configured (skeleton mode)."""
    return {"ok": False, "configured": False, "detail": GRAPH_SKELETON_DETAIL}
```

## 7. 测试

新增 `tests/test_sop_search.py`（client 层，用 spec-02 的 fake driver）：

1. **精确命中单个** → `find_sop_events` 返回 1 个候选、`match == "exact"`；只发出**一条**
   查询（断言 fake driver 只被调用一次，模糊查询未执行）。
2. **精确零命中 → 模糊降级** → 发出两条查询，第二条是 `FIND_EVENTS_FUZZY`，
   `match == "fuzzy"`。
3. **两段都零命中** → 空列表 + `match == "none"`，且**不发第三条查询**。
4. **展树**：预置 Event + 3 Step + 2 Output + 分支边 → `SOPTree.nodes` 长度正确、
   `kind` 分类正确、分支边 `condition == "oper_state=up"`、直连边 `condition is None`。
5. **深度截断**：node 行数 > `MAX_SOP_NODES` → `truncated is True` 且 `nodes` 长度
   == `MAX_SOP_NODES`。
6. **环形图不挂**：fake driver 返回带环的节点/边集合 → 方法正常返回（真正的防环在 Cypher
   的深度上限，此处验证 Python 侧去重与组装不会无限循环）。
7. **树里没有 Event 节点**（脏数据）→ 抛 `GraphQueryError`，且 `str(exc)` 是安全消息、
   `detail` 含 db/event_id 上下文。
8. **db 隔离（重点，三条）**
   - 发现阶段：断言 `run_read` 收到 `db_tag is None` 且 `allow_cross_db is True`，
     且 params 里 `_db is None`（不是缺失——否则真库会报 `ParameterMissing`）；
   - 发现阶段收窄：`db="lib_a"` → `db_tag == "lib_a"`、`allow_cross_db is False`；
   - 展树阶段：`get_sop_tree(db="lib_a", ...)` 下传的 `db_tag == "lib_a"`，且两条查询
     （nodes + edges）**都**带 `_db`。
9. **跨库同名不并树**：fake driver 按 `params["_db"]` 返回不同数据；`lib_a` 与 `lib_b`
   各有 `event_id="E1"`，分别展树 → 两棵树的节点集合不相交。
10. **`max_depth` 内联**：断言生成的 cypher 文本含 `*1..5`（传 5 时），且工具层传 999 时
    在校验阶段就返回 detail、**不生成任何 cypher**。

新增 `tests/test_sop_tools.py`（工具层，走 MCP 会话）：

1. `list_tools` 里 `search_sop` 的 description 含 `search_command_template`（两工具闭环的
   提示不能丢）。
2. 入参全空 → `ok is False`、detail 提示四个参数之一；只给 `db` 同样被拒。
3. `limit=0` / `limit=51` / `max_depth=0` / `max_depth=21` 四个边界 → detail 提示范围。
4. 未配置图库（`NEO4J_URI` 空）→ `configured is False`、detail == `GRAPH_SKELETON_DETAIL`。
5. `GraphError` 路径 → `ok is False`、`configured is True`、detail 是安全消息，
   **断言 detail 不含 bolt URI / 驱动异常串**。
6. 非 `GraphError` 异常路径（fake client 抛 `RuntimeError`）→ `unexpected_payload()`，
   detail == `"Unexpected server error."`。
7. 单命中 → `mode == "tree"` 且含 `event` / `nodes` / `edges` / `next_step_hint`；
   多命中 → `mode == "candidates"` 且每个候选含 `db` 与 `event_id`；
   零命中 → `ok is True` 且 `mode == "empty"`。

`tests/conftest.py` 扩展 `make_session`，支持注入 graph client
（`graph_client_factory=lambda: GraphClient(settings, driver=fake)`）。

## 8. 验收

- `uv run pytest` 全绿，覆盖率 ≥ 80%。
- `uv run ruff check .`、`uv run mypy` 干净。
- 有真实 Neo4j 时的手工验证（可加进 `scripts/` 的 live 脚本）：
  1. 造两个逻辑库 `lib_a` / `lib_b`，各放一棵含分支的 SOP 树，且**故意造一条跨库脏边**；
  2. `search_sop(fault_type="链路中断")` → 返回 2 个候选，各带自己的 `db`；
  3. 用 `lib_a` 的 `(db, event_id)` 展树 → 节点全部来自 `lib_a`，**跨库脏边对面的节点不出现**
     （这条是 `ALL(...)` 中间节点过滤是否写对的判定实验）;
  4. 每个 step 的 `action` 能在 spec-04 的模板库里查到命令。
- **文档同步**：`AGENTS.md` 与 `CLAUDE.md`
  - §3 目录骨架的 `app/tools/` 行补 `sop_tools.py`；
  - 图库小节（spec-02 新增的那节）追加 SOP schema 表、两阶段 db 语义、`ALL(...)` 中间节点
    过滤这条陷阱、变长上界不可参数化这条陷阱；
  - §8 已落地业务方法清单追加 `find_sop_events` / `resolve_sop_event` / `get_sop_tree`。
