# spec-05：search_sop 对齐真实图库 schema 改造

> 状态：已定稿（业务逻辑改造指导），交由 `/rdp-implementation docs/spec-05-search_sop-真实图库对齐改造.md` 按 TDD 实施。
>
> 本 spec **取代 spec-03 的 schema / Cypher / models / 工具信封章节**（spec-03 其余章节——分层、错误映射、守卫机制、骨架模式——仍然有效）。实施时凡两份 spec 冲突处，一律以本 spec 为准。

## 0. 背景与动机

spec-03 的 schema（节点属性 `_db`/`id`/`action`/`observation`/`answer`、单一 `:NEXT` 关系）是基于规划推断的。真实图库的示例代码（`Neo4jHelper`、`Neo4jMermaidConverter`、workflow next-node JSON 构建器）证明真实 schema 不同，现按真实契约改造 `search_sop` 业务逻辑。

**范围红线**：

- **不改** Neo4j 驱动层结构（`app/common/neo4j.py` 的类、`run_read` 调用形状、守卫流程、托管事务、日志）；仅一处参数键对齐（§2），属闭环小改动。
- **不改** `search_sop` 的参数签名（spec-01 契约冻结）与注册面。
- spec-01（占位）、spec-02（驱动契约主体）、spec-04（命令模板库）不受影响。
- Mermaid 渲染 **out-of-scope**（用户确认：返回 JSON 即可，渲染交由 agent/前端）。

## 1. 真实图库 schema（事实基础，来自示例代码）

### 1.1 节点

| label | 关键属性 | 说明 |
|---|---|---|
| `Event` | `name`（必有）、`aliases`、`fault_type`/`intent`/`description`（**可能存在，不保证**） | 检索/定位锚点；示例按 `name` 匹配，`Event` 无保证的 `id` 属性 |
| `Step` | `name`（人读）、`Action`（**首字母大写**）、`Observation`（**首字母大写**） | 命令模板匹配键是 `Action` 的值（小写蛇形，与 spec-04 模板库一级 key 的 action 词汇一致） |
| `Output` | `FinalAnswer`（**首字母大写**，`IS NOT NULL` 判定终结）、可选 `name` | SOP 终结输出 |

属性大小写双写在真实数据中并存（示例 JSON 构建器对 `Action/action`、`FinalAnswer/final_answer/reason` 均做 fallback），client 取值必须双写兼容（§4）。

### 1.2 逻辑库隔离

属性名是 **`database`**（不是 `_db`），且**节点和关系上都带**。示例的路径过滤同时写：

```cypher
all(n IN nodes(path) WHERE n.database = ...) AND all(r IN relationships(path) WHERE r.database = ...)
```

展树锁库必须两个都过滤（§3.3）——这是相对 spec-03 的新增防御点。

### 1.3 边

| 关系类型 | 语义 | 属性 |
|---|---|---|
| `Sequence` | 直连（顺序下一步） | 无 `Condition` |
| `Branch` | 条件分支 | 带 `Condition`（首字母大写，如 `oper_state=up`） |

取代 spec-03 的单一 `:NEXT {condition}`。

### 1.4 展树语义

示例代码的展树是「Event → 全部 FinalAnswer 的完整路径集合」：

```cypher
MATCH (start:Event {name: ...})
MATCH path = (start)-[*]->(end)
WHERE end.FinalAnswer IS NOT NULL
```

本 spec 保留 spec-03 的**两步法**（先收可达节点集、再集合内收边）而非路径展开——路径展开在菱形结构上指数膨胀——但把过滤对齐真实 schema（§3.3），结果集合等价。

## 2. 驱动层：唯一一处改动（参数键 `_db` → `database`）

`app/common/neo4j.py`：

```python
# 改前
_DB_PARAM: Final[str] = "$_db"
# ... merged["_db"] = db_tag

# 改后
_DB_PARAM: Final[str] = "$database"
# ... merged["database"] = db_tag
```

同步点（同文件内）：模块 docstring、`_check_db_scope` 报错文案中的参数名表述、`run_read` docstring。

**机制不变**：`db_tag` 必填、非 None 注入并覆盖调用方预置值、要求 Cypher 文本含 `$database` 过滤、`db_tag=None` 必须显式 `allow_cross_db=True`、违反守卫抛 `ValueError`。

**跨库调用形式相应改为** `{"database": None}`（Cypher 写 `($database IS NULL OR ...)`，参数缺失真库报 `ParameterMissing`，此约定不变，只换键名）。

**联动改动**：`app/graph/client.py` 所有预置 `{"_db": None}` 处改为 `{"database": None}`；所有断言 Cypher 含 `$_db` 的测试改为 `$database`；fake driver 记录的 params 断言同步换键。

## 3. `app/graph/cypher.py` 重写

### 3.1 常量块

```python
LABEL_EVENT: Final = "Event"
LABEL_STEP: Final = "Step"
LABEL_OUTPUT: Final = "Output"

REL_SEQUENCE: Final = "Sequence"   # 取代 REL_NEXT
REL_BRANCH: Final = "Branch"

DB_PROPERTY: Final = "database"    # 真实属性名；节点与关系共用

PROP_ACTION: Final = "Action"
PROP_OBSERVATION: Final = "Observation"
PROP_FINAL_ANSWER: Final = "FinalAnswer"
PROP_CONDITION: Final = "Condition"

# 双写兼容取值表达式（Cypher 片段，供查询模板内联）
ACTION_EXPR: Final = "coalesce(n.Action, n.action, '')"
OBSERVATION_EXPR: Final = "coalesce(n.Observation, n.observation, '')"
FINAL_ANSWER_EXPR: Final = "coalesce(n.FinalAnswer, n.final_answer, n.reason, '')"

MAX_SOP_DEPTH: Final = 20
MAX_SOP_NODES: Final = 200
MAX_SOP_CANDIDATES: Final = 50
```

### 3.2 发现阶段

`FIND_EVENTS_EXACT` / `FIND_EVENTS_FUZZY` 结构与 spec-03 一致，两处调整：

1. 所有 `$_db` → `$database`（守卫要求）；
2. 匹配分支里的属性访问改为 coalesce 防缺属性误伤：

```cypher
($fault_type IS NOT NULL AND toLower(coalesce(e.fault_type, '')) = $fault_type)
OR ($intent IS NOT NULL AND toLower(coalesce(e.intent, '')) = $intent)
OR ($needle IS NOT NULL AND toLower(e.name) = $needle)
OR ($needle IS NOT NULL AND $needle IN [a IN coalesce(e.aliases, []) | toLower(a)])
```

`($database IS NULL OR e.database = $database)` 库过滤子句不变（只换参数名）。

**定位键改为 `(db, event_name)`**：真实 Event 用 `name` 定位（示例即 `MATCH (start:Event {name: ...})`）。RETURN 回显：

```cypher
RETURN e.database AS db, e.name AS event_name,
       coalesce(e.id, elementId(e)) AS event_id,   -- 稳定内部 id：优先业务 id，缺失退元素 id
       coalesce(e.fault_type, '') AS fault_type,
       coalesce(e.intent, '') AS intent
ORDER BY db, event_name
LIMIT $limit
```

`RESOLVE_EVENT_BY_NAME`（原 `RESOLVE_EVENT_BY_ID`）：

```cypher
MATCH (e:{LABEL_EVENT})
WHERE ($database IS NULL OR e.database = $database)
  AND toLower(e.name) = $event_name
RETURN ... -- 同上
ORDER BY db, event_name
```

> `event_id`（`coalesce(e.id, elementId(e))`）仍回显：供跨会话引用与调试；**定位一律走 name**，因为真实 Event 的 `id` 不保证存在。

### 3.3 展树阶段（两步法 + 关系过滤加强）

`RESOLVE`/定位后，`sop_tree_nodes(max_depth)`：

```cypher
MATCH (e:{LABEL_EVENT})
WHERE e.database = $database AND toLower(e.name) = toLower($event_name)
OPTIONAL MATCH path = (e)-[:{REL_SEQUENCE}|{REL_BRANCH}*1..{max_depth}]->(m)
WHERE ALL(n IN nodes(path) WHERE n.database = $database)
  AND ALL(r IN relationships(path) WHERE r.database = $database)   -- 相对 spec-03 的新增
WITH e, collect(DISTINCT m) AS reached
UNWIND ([e] + reached) AS n
WITH DISTINCT n WHERE n IS NOT NULL
RETURN coalesce(n.id, elementId(n)) AS id, labels(n) AS labels,
       coalesce(n.name, '') AS name,
       {ACTION_EXPR 内联，对 n} AS action,
       {OBSERVATION_EXPR 内联，对 n} AS observation,
       {FINAL_ANSWER_EXPR 内联，对 n} AS final_answer,
       properties(n) AS props
LIMIT $node_limit
```

> 注：`ACTION_EXPR` 等片段把节点变量写死为 `n`；此处复用恰好一致。实现时直接内联字符串即可，不新增参数化机制。

`SOP_TREE_EDGES`（集合内收边，含关系库过滤与类型回显）：

```cypher
MATCH (a)-[r:{REL_SEQUENCE}|{REL_BRANCH}]->(b)
WHERE a.database = $database AND b.database = $database AND r.database = $database
  AND coalesce(a.id, elementId(a)) IN $node_ids
  AND coalesce(b.id, elementId(b)) IN $node_ids
RETURN coalesce(a.id, elementId(a)) AS source,
       coalesce(b.id, elementId(b)) AS target,
       type(r) AS rel_type,
       r.Condition AS condition          -- Sequence 边为 null
ORDER BY source, target
```

变长上界内联的三重防护（spec-03 §4.4）原样保留：工具层 `positive_bound_detail` → client `int()` + 钳位 `[1, MAX_SOP_DEPTH]` → `sop_tree_nodes` 是唯一内联点。

## 4. `app/graph/models.py` 与 client 取值

- `SOPEdge`：新增 `rel_type: str = "Sequence"`；`condition: str | None = None` 保留（Sequence 边 null，Branch 边回显 `Condition`）。
- `SOPNode`：保留 `id`/`kind`/`name`/`action`/`observation`；`answer` 字段更名 `reason`（对齐示例 JSON 语义，Output 的终结文案）。`kind` 取值仍由 labels 推导（小写）。
- `SOPCandidate`：`event_id` 改为 `event_name`；内部 `event_id`（`coalesce(e.id, elementId(e))`）经 `extra="allow"` 保留。
- `SOPTree`：结构不变（`db`/`event`/`nodes`/`edges`/`truncated`）。

client 侧：`_to_node` 的行字段名随 Cypher RETURN 别名（`action`/`observation`/`final_answer`）调整——Cypher 已做双写 coalesce，client 不再需要二次 fallback（§3.1 的表达式就是双写兼容的实现位置）。

## 5. 工具信封（JSON 形状，对齐示例 `build_node_object`/`build_response_data`）

`search_sop` 的三种 mode 语义不变（tree/candidates/empty；零命中 `ok: True`；未配置 `graph_skeleton_payload()`），信封字段按示例 JSON 形状调整：

```jsonc
// mode = "tree"
{
  "ok": true, "configured": true, "mode": "tree",
  "match": "exact",                    // exact | fuzzy | direct
  "db": "ospf_down",
  "event": {"event_name": "bgp邻居down", "event_id": "...", "fault_type": "", "intent": ""},
  "nodes": [
    {"id": "...", "name": "检查接口状态", "type": "function_call", "label": "Step",
     "action": "verify_interface_state", "observation": "oper_state"},
    {"id": "...", "name": "", "type": "final_answer", "label": "Output",
     "reason": "接口物理 down，报修线路..."}
  ],
  "edges": [
    {"source": "...", "target": "...", "rel_type": "Sequence", "condition": null},
    {"source": "...", "target": "...", "rel_type": "Branch", "condition": "oper_state=up"}
  ],
  "truncated": false,
  "next_step_hint": "Each Step's 'action' is the key for search_command_template."
}
```

节点 JSON 构建规则（照搬示例 `determine_node_type`/`build_node_object`）：

- `type`：label 含 `Output` 或 `final_answer` 非空 → `"final_answer"`（内容进 `reason`）；否则（`Step` / 有 `Action`）→ `"function_call"`（`action`/`observation` 有值才输出，空串省略）；Event 根节点同样按此规则（通常落 `function_call` 且无 action）。
- `label`：labels 首个值原样回显。
- 其余属性：`properties(n)` 中的未知字段经 `extra="allow"` 保留（瘦类型规则不变），但**不回显到信封顶层**，避免噪音。

candidates / empty 信封形状不变，仅 `event_id` → `event_name`；`next_step_hint` 文案相应改为「用候选的 db + event_name 重调」。

## 6. 测试要点（TDD 红线）

1. 守卫换键：`run_read` 注入 `params["database"]`；断言 Cypher 文本含 `$database`；`{"_db": ...}` 预置不再被识别（回归测试防漏改）。
2. 跨库发现：params 显式含 `database: None`；给了 `db` 时 `db_tag=db` 不跨库。
3. 展树锁库：fake driver 断言节点查询与边查询文本都含 `r.database`/`relationships(path)` 的关系过滤（防 spec-03 旧查询回归）。
4. 节点 JSON 双写兼容：fixture 分别给 `{Action: ...}` 与 `{action: ...}` 的节点，信封 `action` 值相同；`FinalAnswer`/`final_answer`/`reason` 三写同理。
5. 边语义：Sequence 边 `condition is None`；Branch 边 `condition == "oper_state=up"` 原样透传；`rel_type` 正确。
6. 跨库脏数据：节点与**关系**任一缺 `database` 属性都不得进树（两组 fixture）。
7. 定位：同名 Event 跨库命中多条 → candidates；`(db, event_name)` 唯一 → 直展。
8. 信封：`type`/`label`/`reason` 字段存在性按 §5 规则断言；`truncated` 触发（node_limit）行为不变。

## 7. 与 spec-03 的逐条差异清单（供 rdp 核对）

| # | spec-03（旧） | spec-05（新） |
|---|---|---|
| 1 | 逻辑库属性 `_db`，仅节点 | 属性 `database`，**节点 + 关系** |
| 2 | 注入参数 `$_db` | `$database`（`_DB_PARAM` 同步） |
| 3 | 关系 `:NEXT {condition}` | `:Sequence` / `:Branch {Condition}` |
| 4 | Step 属性 `action`/`observation`（小写） | `Action`/`Observation`（双写兼容取值） |
| 5 | Output 属性 `answer` | `FinalAnswer`（`final_answer`/`reason` 兜底），模型字段更名 `reason` |
| 6 | 定位键 `(db, event_id)` | `(db, event_name)`；`event_id` 降级为回显字段 |
| 7 | Cypher 直接访问 `e.fault_type`/`e.intent` | coalesce 防缺属性 |
| 8 | 边查询仅节点库过滤 | 增加 `r.database` 关系库过滤 |
| 9 | 节点信封 `kind`/`answer` | JSON 形状 `type`（function_call/final_answer）/`label`/`reason` |
| 10 | — | Mermaid 渲染明确 out-of-scope |

## 8. 文档联动（实施完成后同步，两文件逐字节相同）

`AGENTS.md` / `CLAUDE.md` §8「图库（Neo4j）」：

- SOP schema 表 → 真实命名（`Step.Action/Observation`、`Output.FinalAnswer`、`Sequence/Branch`、`database` 属性在节点与关系上）；
- 「`_db` 逻辑库守卫」段落 → 参数名 `database`，补「关系也带 database 过滤」；
- 两阶段 db 语义段落 → `$database`、定位键 `(db, event_name)`、跨库 params 预置 `{"database": None}`；
- 信封语义段落 → 补 JSON 形状要点（type/label/reason、rel_type）。

## 9. 实施指引

本 spec 交由 `/rdp-implementation docs/spec-05-search_sop-真实图库对齐改造.md` 按 TDD（RED→GREEN→REFACTOR）实施。建议任务顺序：

1. 驱动换键（§2）+ 守卫/跨库测试改键——最小闭环，先行；
2. `cypher.py` 常量与五条查询重写（§3）；
3. `models.py` + client 取值调整（§4）；
4. 工具信封 JSON 形状（§5）+ `sop_tools` 描述串同步；
5. 全量回归（`uv run pytest` / `ruff` / `mypy` 全绿，覆盖率 ≥ 80%）；
6. §8 文档联动。
