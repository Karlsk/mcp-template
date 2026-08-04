# Spec 02：Neo4j 驱动与图库配置

> 状态：待实施
> 依据：`AGENTS.md`（= `CLAUDE.md`）§4 分层架构、§6 HttpClient 规范（作镜像参照）、§8.2 接入新类型数据源
> 范围：`neo4j` 依赖、`app/common/neo4j.py` 通用驱动、`app/graph/` 集成层骨架、settings/YAML 配置、lifespan 接线、networkx 必要性判断
> 前置：spec-01（占位工具已注册）
> 后置：spec-03（`search_sop` 依赖本 spec 的 `GraphClient` 与 `_db` 调用约定）

## Context

SOP 图库存放在 Neo4j。仓库目前只有 HTTP 一种外部依赖（`app/common/http.py` +
`app/sdn/`），本 spec 按**完全相同的分层形状**引入第二种外部依赖，使既有约束（导入方向、
错误脱敏、注入接缝、骨架模式）零例外地延续到图库。

一个关键的部署事实决定了本 spec 的核心设计：**Neo4j 社区版不支持多 database**。因此逻辑上的
多库通过节点上的 `_db` 属性实现——每个逻辑库承载一组 SOP，每组内可含多个 Event（每个 Event
一棵树）。`_db` 的作用是**把图遍历锁在单一逻辑库内，避免不同 SOP 的图互相串连**。

## 设计决策

### 1. 分层：`common/neo4j.py`（通用）+ `app/graph/`（集成层）

```
┌───────────────────────────────────────────────────────────┐
│ app/tools/sop_tools.py     MCP 工具：薄适配器               │
├───────────────────────────────────────────────────────────┤
│ app/graph/     GraphClient：Cypher/参数/结果解析/错误映射    │
│   client.py    cypher.py（标签与语句常量）                  │
│   models.py    exceptions.py（GraphError 层级）             │
├───────────────────────────────────────────────────────────┤
│ app/common/neo4j.py  通用 Neo4jClient：会话/事务/超时/日志   │
├───────────────────────────────────────────────────────────┤
│ neo4j AsyncDriver → Neo4j server                           │
└───────────────────────────────────────────────────────────┘
```

导入方向（与 §4 同构，不得反向）：

- `tools` → `graph`（client + exceptions + models）；**禁止** tools 层 `import neo4j` 或
  `from app.common.neo4j import ...`。
- `graph.client` → `app.common.neo4j` + `graph.models` + `graph.cypher` + `graph.exceptions`；
  **只有 `graph/client.py` 处理 `neo4j.exceptions.*`**，并映射为 `GraphError` 子类。
- `common.neo4j` 与 SOP 完全无关（integration-agnostic）：不含标签名、Cypher 语句、业务模型。
- `graph.models` 是纯数据：不 import 内部模块，无 IO。

理由与 §4 一致：MCP 会把未捕获异常的 `str()` 作为 `isError` 文本回传给模型，而 neo4j 驱动的
异常串包含 bolt URI、Cypher 片段与服务端 stacktrace 摘要。分层 + 脱敏异常层级保证**到达
模型的永远是人工撰写的安全消息**。

### 2. 依赖

`pyproject.toml` `[project.dependencies]` 追加：

```toml
    "neo4j>=5.28",
```

用官方驱动的异步接口 `neo4j.AsyncGraphDatabase`。若 `uv run mypy` 报缺类型存根，追加：

```toml
[[tool.mypy.overrides]]
module = ["neo4j.*"]
ignore_missing_imports = true
```

（先跑一次 mypy 再决定加不加——官方驱动自带 `py.typed`，多数版本不需要。）

### 3. 逻辑多 database（`_db`）—— 每次 query 现填

这是本 spec 最重要的契约。

```python
async def run_read(
    self,
    cypher: str,
    params: dict[str, Any] | None = None,
    *,
    db_tag: str | None,
    allow_cross_db: bool = False,
    query_name: str = "query",
) -> list[dict[str, Any]]:
```

规则：

1. **`db_tag` 是关键字必填参数，没有默认值**。调用方每次显式给出——逻辑库标识是数据的一部分
   （由发现查询回显 `e._db` 得到），不是进程级常量。
2. `db_tag` 非 None 时，client 注入 `params["_db"] = db_tag`，并**在执行前断言 cypher 文本
   包含 `$_db`**，否则抛 `ValueError`。这把"忘写逻辑库过滤"从"静默跨库串图"变成"确定性失败"。
3. `db_tag=None` 表示**有意跨库**（只在 Event 发现阶段用到），此时必须同时传
   `allow_cross_db=True`，且跳过 `$_db` 断言。两个参数一起写才能跨库 ⇒ 跨库永远是显式决定，
   review 时 grep `allow_cross_db` 就能审计全部跨库点。
4. **配置里不存 `db_tag`**：`Neo4jSettings` 没有该字段。写进配置就等于把"每次现填"退化成
   "全局默认"，第一次要查另一个逻辑库时就会被迫绕过守卫。
5. `query_name` 是日志用的短名（见 §12），不影响查询语义；业务方法一律传入（如
   `"find_sop_events_exact"`），让日志可定位而无需输出 Cypher 全文。

```python
_DB_PARAM: Final[str] = "$_db"

def _check_db_scope(cypher: str, db_tag: str | None, allow_cross_db: bool) -> None:
    """Fail fast unless the query is either db-scoped or explicitly cross-db."""
    if db_tag is None:
        if not allow_cross_db:
            raise ValueError(
                "db_tag is None but allow_cross_db is False: a cross-logical-db "
                "query must be requested explicitly."
            )
        return
    if _DB_PARAM not in cypher:
        raise ValueError(
            f"cypher must filter on {_DB_PARAM} when db_tag is given "
            "(logical database isolation)."
        )
```

`ValueError` 而非 `GraphError`：这是**开发期编程错误**，不是运行期外部故障，应当在测试里被
钉死、不该有"安全消息"路径。工具层的 `except Exception` 兜底保证它不会泄漏给模型。

### 4. 事务与重试：交给驱动，不自实现退避

`HttpClient` 自带 `RetryConfig`（指数退避 + full jitter），因为 httpx 不管重试。**Neo4j 驱动
不同**：托管事务 `session.execute_read(...)` 内建瞬时错误（leader 切换、连接抖动）重试，
重试预算由 `max_transaction_retry_time` 控制。

因此：

- 统一用 `session.execute_read(work)` 执行只读查询，**不写自己的重试循环**；
- 只读一律走 `execute_read`（可路由到副本，且驱动知道它可安全重试）；
- 本 spec **不提供写入方法**——SOP 图的写入属数据治理流程，不经 MCP 暴露。`run_read` 的命名
  即契约。

```python
async def run_read(self, cypher, params=None, *, db_tag, allow_cross_db=False,
                   query_name="query"):
    _check_db_scope(cypher, db_tag, allow_cross_db)
    merged = dict(params or {})
    if db_tag is not None:
        # db_tag wins over any caller-supplied `_db`: the scope is decided by the
        # explicit argument, never by a leftover value inside `params`.
        merged["_db"] = db_tag
    driver = await self._get_driver()
    started = time.monotonic()
    async def _work(tx: AsyncManagedTransaction) -> list[dict[str, Any]]:
        result = await tx.run(cypher, merged, timeout=self._config.query_timeout)
        return [record.data() async for record in result]
    async with driver.session(database=self._config.database) as session:
        records = await session.execute_read(_work)
    # ... 日志
    return records
```

`record.data()` 把节点/关系摊平成普通 dict，因此 `run_read` 的返回类型是
`list[dict[str, Any]]`——**通用层不返回驱动对象**，`graph` 层拿到的就是纯 JSON-like 结构，
可直接喂 Pydantic，也天然满足"models 层无外部依赖"。

### 5. 配置

`config/sdn_controller.yaml` 追加顶层 `neo4j:` 块（沿用同一个配置文件：`SDN_CONFIG_FILE`
已可覆盖路径，再加第二个文件只会多一套加载/校验逻辑）：

```yaml
# SOP graph database (NON-SECRET). Credentials come from env/.env
# (NEO4J_USERNAME / NEO4J_PASSWORD), never here. uri comes from NEO4J_URI (env);
# leave it empty below. Empty uri = skeleton mode (graph tools return configured:false).
neo4j:
  uri: ""                        # overridden by NEO4J_URI at runtime, e.g. bolt://127.0.0.1:7687
  database: "neo4j"              # PHYSICAL database; community edition has exactly one.
                                 # Logical databases are the node `_db` property, passed
                                 # per query by the graph layer (never configured here).
  query_timeout: 30.0
  max_transaction_retry_time: 15.0
  log_params: false              # DEBUG only: log query parameters
```

`app/settings.py`：

```python
class Neo4jSettings(BaseModel):
    """Non-secret SOP graph configuration (loaded from YAML).

    Note: there is deliberately no ``db_tag`` here. The logical database (the
    node ``_db`` property) is data, not configuration — it is resolved per query
    by ``app/graph`` and passed explicitly on every call.

    ``extra="forbid"`` so that credentials mistakenly written under the ``neo4j:``
    block (e.g. ``username:``) fail loudly at startup instead of being silently
    ignored — they belong in env/.env only.
    """

    model_config = ConfigDict(extra="forbid")

    uri: str = ""
    database: str = "neo4j"
    query_timeout: float = 30.0
    max_transaction_retry_time: float = 15.0
    log_params: bool = False
```

`Settings` 追加：

```python
    # ---- Neo4j SOP graph connection (env / .env) ----
    neo4j_uri: str | None = None
    neo4j_username: str | None = None
    neo4j_password: SecretStr | None = None

    # ---- Neo4j non-secret structure (YAML) ----
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)

    @property
    def neo4j_base_uri(self) -> str:
        """Effective graph URI — env override wins over the YAML value."""
        return self.neo4j_uri or self.neo4j.uri

    def neo4j_is_configured(self) -> bool:
        """True when a graph URI is present."""
        return bool(self.neo4j_base_uri)
```

`_FORBIDDEN_YAML_KEYS` 追加两个键，让 YAML 拒密钥守卫覆盖图库凭据：

```python
_FORBIDDEN_YAML_KEYS = frozenset(
    {
        "sdn_controller_token",
        "sdn_controller_username",
        "sdn_controller_password",
        "neo4j_username",
        "neo4j_password",
    }
)
```

两处配套修正（容易漏）：

1. `_reject_secrets_in_yaml` 的报错文案当前写死了 `SDN_CONTROLLER_*` 三个变量名，需改成
   不枚举具体名字的通用文案（如 "Provide them via environment or .env instead."），
   否则写了 `neo4j_password` 的人会看到一条误导的提示。
2. `_collect_keys` 是**递归**收集所有层级的 key，所以 `neo4j_password` 写在任何嵌套位置都会被
   拦住；但 `neo4j: {password: ...}` 这种写法的 key 是 `password`，守卫拦不到——这正是上面给
   `Neo4jSettings` 加 `extra="forbid"` 的原因：它会在启动时直接 `ValidationError`，
   而不是默默忽略、让密码遗留在可提交的 YAML 里。

`.env.example` 追加（不带真实值）：

```
# ---- SOP graph (Neo4j) ----
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USERNAME=
NEO4J_PASSWORD=
```

### 6. `app/common/neo4j.py`（新文件）

```python
"""Async Neo4j client with managed read transactions and query logging.

Generic and integration-agnostic: it knows nothing about SOP labels, Cypher
statements, or response models — those live in ``app/graph``.

Design notes:
- Retries are delegated to the driver's managed transactions
  (``session.execute_read``), which retry transient failures within
  ``max_transaction_retry_time``. Unlike ``HttpClient`` there is no hand-rolled
  backoff loop here; re-implementing one would fight the driver.
- Logical multi-database: the community edition has a single physical database,
  so tenancy is a node property (``_db``). ``run_read`` requires the caller to
  pass ``db_tag`` on EVERY call and asserts the query filters on ``$_db``;
  crossing logical databases requires an explicit ``allow_cross_db=True``.
- Accepts an injectable ``driver`` so query construction and error mapping can be
  unit-tested without a real server (mirrors ``HttpClientConfig.transport``).

Security note: structured ``neo4j_query`` / ``neo4j_result`` / ``neo4j_error``
records are server-side only. The password is never logged; query parameters are
logged only when ``log_params`` is set (DEBUG), since they may carry device
identifiers. Do not forward these logs to a channel the model can read.
"""
```

```python
@dataclass
class Neo4jClientConfig:
    """Neo4j client configuration.

    ``database`` is the PHYSICAL Neo4j database (community edition: always
    "neo4j"). Logical databases are not configured here — see ``run_read``.
    """

    uri: str = ""
    username: str = ""
    password: str = ""
    database: str = "neo4j"
    query_timeout: float = 30.0
    max_transaction_retry_time: float = 15.0
    # Injectable driver (e.g. a fake) so query logic is unit-testable.
    driver: AsyncDriver | None = None
    log_params: bool = False


class Neo4jClient:
    """Async Neo4j client exposing read-only, db-scoped query execution."""

    def __init__(self, config: Neo4jClientConfig | None = None) -> None: ...

    @property
    def configured(self) -> bool: ...          # bool(uri) or injected driver

    async def __aenter__(self) -> Neo4jClient: ...
    async def __aexit__(self, *exc_info: object) -> None: ...

    async def _get_driver(self) -> AsyncDriver:
        """Lazily create (or return the injected) driver. Not thread-hostile:
        one client instance is owned by one MCP session."""

    async def verify_connectivity(self) -> None: ...
    async def run_read(self, cypher, params=None, *, db_tag, allow_cross_db=False,
                       query_name="query") -> list[dict]: ...
    async def close(self) -> None:
        """Close the underlying driver. Idempotent."""
```

驱动构造：

```python
AsyncGraphDatabase.driver(
    self._config.uri,
    auth=(self._config.username, self._config.password),
    max_transaction_retry_time=self._config.max_transaction_retry_time,
)
```

**惰性创建**：`_get_driver()` 首次调用时才建 driver。这样"配了 uri 但图库宕机"不会在
lifespan 阶段炸掉整个会话（见决策 7）。

### 7. lifespan 接线与骨架模式

`app/server.py`：

```python
GraphClientFactory = Callable[[], GraphClient]


def default_graph_client_factory(settings: Settings) -> GraphClientFactory:
    def _factory() -> GraphClient:
        return GraphClient(settings)

    return _factory


def _make_lifespan(
    factory: SDNClientFactory,
    graph_factory: GraphClientFactory,
) -> Callable[[FastMCP], AbstractAsyncContextManager[dict[str, object]]]:
    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, object]]:
        client = factory()
        graph = graph_factory()
        try:
            await client.initialize()
            # Unlike the SDN basic-auth login, graph connectivity is NOT
            # fail-fast: SDN tools must keep working when the SOP graph is down.
            await graph.probe()
            yield {"sdn_client": client, "graph_client": graph}
        finally:
            await graph.aclose()
            await client.aclose()

    return lifespan
```

`build_server` 增加 `graph_client_factory: GraphClientFactory | None = None` 注入参数
（与 `sdn_client_factory` 对称，测试接缝）。

**与 SDN basic 的 fail-fast 明确不同**：`GraphClient.probe()` 内部吞掉连接异常、只记
WARNING（`graph_probe_failed`），绝不抛出。理由：

- SDN 工具（设备/链路/告警/性能）与图库无关，图库宕机不应让整个 MCP server 拒绝服务；
- SOP 检索失败对 agent 是"这条线索暂不可用"，可以降级继续排障；
- `uri` 为空即**骨架模式**，`GraphClient.configured is False`，工具返回
  `{"ok": false, "configured": false}`（复用 `validation.skeleton_payload()` 的语义，
  但 detail 文案换成图库版，见 spec-03）。

lifespan 上下文的值类型从 `SDNClient` 变成异构 dict，注意把类型标注放宽为
`dict[str, object]`，工具层取用时按现有写法显式标注：

```python
graph: GraphClient = ctx.request_context.lifespan_context["graph_client"]
```

**驱动生命周期决策**：与 `SDNClient` 完全一致——**每会话一个 `GraphClient`，自持 driver，
lifespan `finally` 关闭**。官方文档建议"每应用一个 driver"（driver 自带连接池），本项目
选择每会话，理由是与既有 lifespan 形状一致、无新生命周期概念、测试注入简单；MCP 会话数量
级低。若将来会话churn 变高，只需把 factory 改成闭包持有的进程级单例 driver，
`GraphClient.aclose()` 改为不关共享 driver——**改动范围限于 `server.py` 的 factory**。

### 8. `app/graph/exceptions.py`（新文件）

与 `app/sdn/exceptions.py` 同构，铁律一致：`__str__` 只返回人工撰写的 `public_message`，
原始异常串放 `detail`（仅服务端日志）。

```python
class GraphError(Exception):
    """Base class for all SOP graph errors."""

    def __init__(self, public_message: str, *, detail: str | None = None) -> None: ...
    def __str__(self) -> str: return self.public_message


class GraphConfigError(GraphError):
    """The SOP graph is not configured (missing URI or credentials)."""


class GraphConnectionError(GraphError):
    """The SOP graph could not be reached (network, timeout, or unavailable)."""


class GraphAuthError(GraphError):
    """Authentication to the SOP graph failed."""


class GraphQueryError(GraphError):
    """The graph rejected the query, or its response was malformed."""
```

`app/graph/client.py` 里集中映射（唯一处理驱动异常的地方）：

```python
def _map_neo4j_error(exc: Exception) -> GraphError:
    """Map a driver exception onto the sanitized graph error hierarchy."""
    if isinstance(exc, neo4j.exceptions.AuthError):
        return GraphAuthError("SOP graph authentication failed.", detail=str(exc))
    if isinstance(exc, neo4j.exceptions.ServiceUnavailable | neo4j.exceptions.SessionExpired):
        return GraphConnectionError("SOP graph is unreachable.", detail=str(exc))
    if isinstance(exc, neo4j.exceptions.ClientError):
        return GraphQueryError("SOP graph rejected the query.", detail=str(exc))
    return GraphError("SOP graph request failed.", detail=str(exc))
```

注意 `AuthError` 是 `ClientError` 的子类，**顺序必须先 AuthError**。同理
`neo4j.exceptions.Neo4jError` 是基类，放最后由兜底分支覆盖。这个顺序要用测试钉死。

### 9. `app/graph/client.py` 骨架（业务方法在 spec-03 填充）

```python
class GraphClient:
    """SOP graph business methods over the generic Neo4j client."""

    def __init__(self, settings: Settings, *, driver: AsyncDriver | None = None) -> None:
        self._settings = settings
        self._neo4j = Neo4jClient(
            Neo4jClientConfig(
                uri=settings.neo4j_base_uri,
                username=settings.neo4j_username or "",
                password=(
                    settings.neo4j_password.get_secret_value()
                    if settings.neo4j_password
                    else ""
                ),
                database=settings.neo4j.database,
                query_timeout=settings.neo4j.query_timeout,
                max_transaction_retry_time=settings.neo4j.max_transaction_retry_time,
                log_params=settings.neo4j.log_params,
                driver=driver,
            )
        )

    @property
    def configured(self) -> bool:
        return self._neo4j.configured

    def _require_configured(self) -> None:
        if not self.configured:
            raise GraphConfigError("SOP graph is not configured.")

    async def probe(self) -> bool:
        """Best-effort connectivity check at startup. Never raises."""

    async def aclose(self) -> None: ...
```

业务方法统一形状（spec-03 照此写）：

```python
    async def some_query(self, ...) -> SomeModel:
        self._require_configured()
        try:
            rows = await self._neo4j.run_read(CYPHER_X, {...}, db_tag=db,
                                              query_name="some_query")
        except Exception as exc:                      # driver exceptions only here
            raise _map_neo4j_error(exc) from exc
        try:
            return SomeModel.model_validate({...})
        except (ValueError, ValidationError) as exc:
            raise GraphQueryError("SOP graph response was malformed.", detail=str(exc)) from exc
```

`except Exception` 而非精确的 `neo4j.exceptions.Neo4jError`：驱动还会抛
`asyncio.TimeoutError`、`OSError` 等非 Neo4jError 异常，全部需要被映射为安全消息。
`_map_neo4j_error` 的兜底分支负责它们。**注意** `ValueError`（`_check_db_scope` 的编程错误
断言）会被这个 `except Exception` 捕获并包装——这是可接受的：工具层照样返回安全消息，而
单元测试直接调 `run_read` 来断言 `ValueError`，契约不丢。

### 10. `app/graph/cypher.py`（新文件）：标签与语句集中管理

所有标签名、关系类型、Cypher 语句常量集中在此。对齐真实图库（若标签命名不同）只改这一个
文件，不动 client 逻辑。语句本身在 spec-03 给出。

```python
LABEL_EVENT: Final = "Event"
LABEL_STEP: Final = "Step"
LABEL_OUTPUT: Final = "Output"
REL_NEXT: Final = "NEXT"
DB_PROPERTY: Final = "_db"
```

### 11. networkx 必要性判断（结论：现阶段不引入）

问题：是否需要把 Neo4j 的图实例化成 `networkx.DiGraph` 再操作？

**结论：不引入。** 论据：

1. **本轮需求不含图算法。** `search_sop` 要做的是"从一个 Event 出发取出它的整棵有界子图"，
   这正是 Cypher 变长路径匹配的原生能力，一次查询即可返回节点集与边集（见 spec-03 的两步
   查询）。没有最短路、中心度、连通分量、社区发现等需要算法库的诉求。
2. **实例化 networkx 等于建第二份真相。** 要么每次请求全量拉图后建图（把一次有界查询变成
   全量扫描 + 内存构建，延迟和内存都更差），要么常驻缓存（引入缓存失效、并发一致性、内存
   增长三个新问题）。SOP 图会被人工编辑，缓存过期会让 agent 拿到旧流程——这是**正确性**
   风险，不只是性能问题。收益为负。
3. **真正可能需要算法的是 `get_fault_subgraph`**（拓扑快照 + 告警的影响面分析，可能要
   k-hop BFS / 最短路 / 割点）。但即便到那时也有更好的两个选项：Cypher 原生
   `shortestPath()` / 变长 BFS，或 Neo4j GDS 库（在数据库内执行，无需搬数据）。且该工具的
   拓扑数据主要来自 SDN 控制器（`get_topology`）而非 Neo4j，届时若要在 Python 侧算，
   输入是控制器 JSON，与本 spec 的图库驱动无关。
4. **留门设计（零成本）：** `GraphClient` 的返回结构统一为序列化中立的节点集 + 边集：

   ```python
   class GraphFragment(BaseModel):
       """Serialization-neutral graph payload: plain nodes + edges.

       Deliberately not a networkx graph — see docs/spec-02 §11. Keeping this
       shape means a NetworkX adapter can be added later in ``app/graph/nx.py``
       without touching the client or the tools.
       """

       nodes: list[dict[str, Any]]
       edges: list[SOPEdge]
       truncated: bool = False
   ```

   将来若确需算法，新增 `app/graph/nx.py::to_digraph(fragment) -> nx.DiGraph` 即可，
   `client.py` / `tools/` 均无需改动，且 networkx 只作为**可选** dev/extra 依赖引入。

把这段判断写进 `AGENTS.md` 的图库小节，避免后来者重复讨论。

### 12. 日志

复用 `app/common/logging.py`（`logging.getLogger(__name__)` 自动挂到 `app.*` 的 JSON handler）。
结构化事件与 `http.py` 对齐：

| 事件 | 级别 | `extra` 字段 |
|---|---|---|
| `neo4j_query` | DEBUG | `query_name`、`db_tag`、`cross_db`、`params`（仅 `log_params=true`） |
| `neo4j_result` | DEBUG | `query_name`、`db_tag`、`record_count`、`elapsed_ms` |
| `neo4j_error` | WARNING | `query_name`、`db_tag`、`error_type`、`elapsed_ms` |

规则：

- **密码永不落日志**（不进 `extra`，不进 driver 构造日志）；
- `params` 仅在 `log_params=true` 且 DEBUG 时输出（参数里可能有设备名/租户标识）；
- Cypher 全文不作为默认字段（长且噪声大）；`query_name` 由调用方传入的短名标识
  （`find_sop_events_exact` / `sop_tree_nodes` / ...），错误定位够用；
- `db_tag` 记录下来很关键：排查"查错逻辑库"类问题时它是第一现场。

## 分步实施

1. `pyproject.toml` 加 `neo4j>=5.28` → `uv sync` → `uv run mypy` 看是否需要 overrides。
2. `app/settings.py`：`Neo4jSettings`（`ConfigDict` 需加进 pydantic 的 import 行）+ `Settings`
   三个 env 字段 + `neo4j` 字段 + `neo4j_base_uri` / `neo4j_is_configured()` +
   `_FORBIDDEN_YAML_KEYS` 两个新键 + `_reject_secrets_in_yaml` 报错文案通用化。
3. `config/sdn_controller.yaml` 追加 `neo4j:` 块；`.env.example` 追加三个变量。
4. `app/common/neo4j.py`：config dataclass + `Neo4jClient` + `_check_db_scope` + 日志。
5. `app/graph/`：`__init__.py`（导出 `GraphClient` + 异常，照 `app/sdn/__init__.py`）、
   `exceptions.py`、`cypher.py`（常量）、`models.py`（`SOPEdge` + `GraphFragment`）、
   `client.py`（构造 / `configured` / `probe` / `aclose` / `_map_neo4j_error`）。
6. `app/server.py`：`GraphClientFactory`、`default_graph_client_factory`、`_make_lifespan`
   双客户端、`build_server` 新注入参数。
7. `tests/conftest.py`：`make_graph_settings()`、fake driver helper、`make_session` 支持
   注入 graph client。
8. 文档同步 `AGENTS.md` + `CLAUDE.md`。

## 测试

新增 `tests/test_neo4j_client.py`：

1. **`_db` 守卫（核心）**
   - `run_read("MATCH (n) RETURN n", db_tag="sop")` → `ValueError`（缺 `$_db`）；
   - `run_read("MATCH (n) WHERE n._db=$_db RETURN n", db_tag="sop")` → 通过，且断言
     **fake driver 实际收到的 params 含 `_db == "sop"`**；
   - `run_read(cypher, db_tag=None)` → `ValueError`（未显式允许跨库，即缺 `allow_cross_db=True`）；
   - `run_read(cypher, db_tag=None, allow_cross_db=True)`（**不传** params）→ 通过，且 client
     **不注入** `_db`（params 里无该键）；
   - `run_read(cypher, {"_db": None}, db_tag=None, allow_cross_db=True)` → 显式传入的
     `_db is None` **原样下传不被删**。这是 spec-03 发现阶段的调用形式：Cypher 文本里
     引用了 `$_db`（`$_db IS NULL OR ...`），参数必须存在且为 null，否则真库报
     `ParameterMissing`。
   - `run_read(cypher, {"_db": "other"}, db_tag="sop")` → 下传 `_db == "sop"`：`db_tag`
     **覆盖**调用方预置值（`merged["_db"] = db_tag` 在 `dict(params)` 之后）。
2. **注入接缝**：fake `AsyncDriver`/`AsyncSession`/`AsyncManagedTransaction` 三件套，
   `execute_read` 直接调用传入的 work 函数；记录收到的 cypher 与 params 供断言。
3. **生命周期**：`close()` 幂等（连调两次不炸）；`async with` 正常进出；未配置
   （`uri=""`、无注入 driver）时 `configured is False`。
4. **日志**：`caplog` 断言成功路径产生 `neo4j_query` + `neo4j_result`，`record_count` 与
   `db_tag` 正确；`log_params=False` 时日志 **不含** `params` 字段。

新增 `tests/test_graph_client.py`：

1. **错误映射顺序**：`AuthError` → `GraphAuthError`（不能落到 `ClientError` 分支）、
   `ServiceUnavailable` → `GraphConnectionError`、`ClientError` → `GraphQueryError`、
   `OSError`/`asyncio.TimeoutError` → `GraphError` 兜底。
2. **脱敏铁律**：对每个映射后的异常断言 `str(exc)` **不含**原始异常串片段（如 URI、
   `Neo4jError` 文本），而 `exc.detail` 含。
3. **骨架模式**：`uri=""` 时 `configured is False`；业务方法抛 `GraphConfigError`。
4. **`probe()` 永不抛**：注入一个 `verify_connectivity` 抛 `ServiceUnavailable` 的 fake
   driver → `probe()` 返回 `False` 且记 WARNING，不抛。

`tests/test_settings.py` 追加：

1. YAML 里写 `neo4j_password` → `Settings()` 抛 `ValueError`（拒密钥守卫生效）；
2. `NEO4J_URI` 环境变量覆盖 YAML 的 `neo4j.uri`（`neo4j_base_uri` 优先级）；
3. `neo4j_is_configured()` 空/非空两态；
4. `Neo4jSettings` **没有** `db_tag` 字段（`assert "db_tag" not in Neo4jSettings.model_fields`）
   ——把"逻辑库不入配置"这条设计决策钉死，防止后人"顺手加个默认值"。
5. `neo4j:` 块里写 `username:` / `password:` → `Settings()` 抛 `ValidationError`
   （`extra="forbid"` 生效，覆盖守卫拦不到的那条路径）。

`tests/conftest.py` 追加：

```python
def make_graph_settings(uri: str = "") -> Neo4jSettings:
    return Neo4jSettings(uri=uri, query_timeout=1.0, max_transaction_retry_time=0.0)
```

以及 fake driver 工厂：记录 `(cypher, params, database)` 调用列表 + 返回预置 records，
供 spec-03 的 SOP 测试复用。

## 验收

- `uv run pytest` 全绿，覆盖率不低于 80%。
- `uv run ruff check .`、`uv run mypy` 干净。
- 无 Neo4j 实例时（`NEO4J_URI` 未设）`uv run sdn-mcp` 正常启动，图库工具返回
  `{"ok": false, "configured": false}`，SDN 工具不受影响。
- 有 Neo4j 实例但账号密码错时，server 仍能启动（只记 WARNING），SOP 工具返回安全消息
  "SOP graph authentication failed."，**且日志外无任何 URI/驱动异常串泄漏**。
- **文档同步**：`AGENTS.md` 与 `CLAUDE.md`
  - §2 技术栈表加一行 `neo4j>=5.28`；
  - §3 目录骨架加 `app/common/neo4j.py` 与 `app/graph/`；
  - §4 分层架构图加图库一列，并补导入方向规则；
  - 新增一节「图库（Neo4j）」：`_db` 每次 query 现填的调用约定、`allow_cross_db` 审计点、
    重试交给驱动、networkx 判断结论（§11 摘要）；
  - §10 环境变量表加 `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD`；
  - §11 排错速查加一条：图库连不上的表现与 `neo4j_error` 日志字段。
