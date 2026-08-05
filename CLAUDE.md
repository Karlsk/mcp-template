# CLAUDE.md — sdn-mcp-template

本文件是 AI 编码代理在本仓库工作的权威指南。所有约定均取自代码现状，修改代码时请同步更新本文件。

---

## 1. 项目背景

一个**可复用、可部署的 MCP (Model Context Protocol) server 模板**，让 AI agent 通过 MCP tools 操作 SDN 网络控制器。

- 基于官方 `mcp` Python SDK 的 **v1 `FastMCP` API**（当前锁定 `mcp==1.28.1`；上游 `main` 的 v2 预发布 API 不兼容，升级前必须核对 API 变更）。
- 主传输为 **Streamable HTTP**（端点 `/mcp`），同时支持 **stdio**（便于 Claude Desktop 等本地客户端联调）。
- 仓库交付的是**骨架**：MCP server、通用 HTTP 客户端、SDN 对接层（结构完整、端点打桩）、配置管理、测试套件与 Docker 部署。接入真实控制器时遵循「models → client → tools」三处改动规范（见 §9）。
- **骨架模式**：`base_url` 留空时 server 照常启动，SDN 工具返回 `{ok: false, configured: false}`。

## 2. 技术栈

| 类别 | 选型 |
|---|---|
| 语言 / 运行时 | Python ≥ 3.13（见 `.python-version`） |
| 包管理 | [uv](https://docs.astral.sh/uv/)（`uv.lock` 锁定；构建后端 hatchling） |
| MCP SDK | `mcp[cli]>=1.28.1`（v1 FastMCP） |
| HTTP | `httpx>=0.28.1`（`AsyncClient`，可注入 `transport`） |
| 图库 | `neo4j>=5.28`（官方 Python 驱动，`AsyncGraphDatabase`，托管事务内建重试；自带 py.typed） |
| 配置 | `pydantic-settings>=2.14.2`（env + YAML 多源合并）、`pyyaml` |
| 服务 | `uvicorn>=0.51.0`（FastMCP 内置 Streamable HTTP 底层） |
| 测试 | `pytest>=8.4`、`pytest-asyncio`（`asyncio_mode=auto`）、`pytest-cov` |
| 质量 | `ruff`（line-length 100，规则集 `E,F,I,UP,B,SIM,RUF`）、`mypy`（`disallow_untyped_defs` 等严格模式） |
| 部署 | 多阶段 Dockerfile（uv builder → python:3.13-slim）+ docker-compose |

入口点：`pyproject.toml` 的 `[project.scripts]` → `sdn-mcp = "app.server:main"`。

常用命令：

```bash
uv sync                 # 安装运行 + dev 依赖
uv run sdn-mcp          # 启动 server（默认 127.0.0.1:8000，/mcp）
uv run pytest           # 271 个用例，--cov-fail-under=80（当前覆盖率 ~97%）
uv run ruff check .     # lint
uv run mypy             # 类型检查（pyproject 已配置 packages=["app"]）
make docker-up          # Docker 构建并启动（见 deploy/）
```

## 3. 项目目录骨架

```
sdn-mcp-template/
├── app/                          # 主包
│   ├── server.py                 # FastMCP 工厂 + per-session lifespan + CLI 入口 (main)
│   ├── settings.py               # pydantic-settings：YAML(非敏感结构) + env/.env(secrets) 合并
│   ├── common/
│   │   ├── http.py               # 通用 HttpClient：retry / auth / REST 封装 + 请求/响应日志（与 SDN 无关）
│   │   ├── neo4j.py              # 通用 Neo4jClient：_db 逻辑库守卫 / 托管事务读取 / 查询日志（与具体图库无关）
│   │   └── logging.py            # 统一日志：JSON 结构化，setup_logging 由 MCP_LOG_LEVEL 驱动 app.* logger
│   ├── graph/                    # SOP 图库集成层（Neo4j）
│   │   ├── client.py             # GraphClient：probe / 骨架守卫 / _map_neo4j_error（唯一处理驱动异常处）
│   │   ├── cypher.py             # 标签/关系/属性常量（LABEL_EVENT / REL_NEXT / DB_PROPERTY …）
│   │   ├── models.py             # SOPEdge / GraphFragment（序列化中立，纯数据）
│   │   └── exceptions.py         # GraphError 层级（安全 public_message）
│   ├── sdn/                      # SDN 集成层
│   │   ├── client.py             # SDNClient：业务方法 + token 生命周期 + 错误映射
│   │   ├── models.py             # Pydantic 响应模型（边界校验，纯数据）
│   │   └── exceptions.py         # SDNError 层级（安全 public_message）
│   └── tools/                    # MCP 工具层（薄适配器）
│       ├── __init__.py           # register_all()：工具模块注册聚合点
│       ├── system.py             # ping、sdn_health
│       ├── sdn_tools.py          # sdn_alerts（旧桩，保留不动）
│       ├── validation.py         # 共享校验 helper（page/time 校验 + 标准信封）
│       ├── device_tools.py       # sdn_device_by_name、sdn_device_by_management_ip
│       ├── link_tools.py         # sdn_link_info
│       ├── topology_tools.py     # sdn_topology
│       ├── perf_tools.py         # sdn_port_traffic / link_performance / vpn / te_tunnel
│       ├── log_tools.py          # sdn_operation_logs
│       ├── alert_tools.py        # sdn_device_alerts（§3.5 新告警工具）
│       ├── cmd_tools.py          # sdn_run_command（设备命令，默认只读 allow_write 覆盖）
│       ├── sop_tools.py          # search_sop（占位，spec-03 落实现）
│       ├── template_tools.py     # search_command_template（占位，spec-04 落实现）
│       ├── graph_tools.py        # get_fault_subgraph / get_topology_snapshot（占位）
│       ├── config_tools.py       # get_config_diff（占位）
│       └── change_tools.py       # get_change_history（占位）
├── config/sdn_controller.yaml    # 非敏感配置（sdn: endpoints / timeout / retry / auth_type；neo4j: uri / database …）
├── scripts/
│   ├── test_client.py            # MCP 测试客户端（list-tools / call-tool）
│   └── test_sdn_live.py          # 对真实控制器的 live 集成验证（basic 全流程）
├── tests/                        # pytest 套件（conftest 提供 MockTransport + fake Neo4j driver + 内存 MCP 会话）
├── deploy/                       # 多阶段 Dockerfile + docker-compose.yml
├── Makefile                      # docker-build/up/stop/logs 等封装
└── .env.example                  # 环境变量模板（真实 .env 已被 gitignore）
```

## 4. 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│ app/server.py      FastMCP 工厂 / lifespan / CLI             │
│ app/settings.py    Settings（YAML + env 合并，SecretStr）     │
├─────────────────────────────────────────────────────────────┤
│ app/tools/         MCP 工具：薄适配器，无业务逻辑              │
├─────────────────────────────────────────────────────────────┤
│ app/sdn/           SDNClient：端点/请求体/响应解析/错误映射     │
│   client.py        exceptions.py（SDNError 层级）             │
│   models.py        Pydantic 响应模型（被 client 和 tools 引用） │
│ app/graph/         GraphClient：Cypher/逻辑库守卫/错误映射      │
│   client.py        exceptions.py（GraphError 层级）           │
│   cypher.py        标签/关系常量   models.py（SOPEdge 等）     │
├─────────────────────────────────────────────────────────────┤
│ app/common/http.py 通用 HttpClient：重试/超时/鉴权/方法封装     │
│ app/common/neo4j.py 通用 Neo4jClient：_db 守卫/读取/查询日志   │
├─────────────────────────────────────────────────────────────┤
│ httpx → SDN controller        neo4j driver → Neo4j 图库      │
└─────────────────────────────────────────────────────────────┘
```

**导入方向规则（强约束，不得反向）：**

- `tools` → `sdn`（client + exceptions）+ `models`；**禁止** import `httpx`、`app.common.http`。
- `sdn.client` → `app.common.http` + `sdn.models` + `sdn.exceptions`；仅 `client.py` 处理 httpx 异常（`except httpx.HTTPError`），将其映射为 `SDNError`。
- `common.http` 与 SDN 完全无关（integration-agnostic）：不含端点、模型、脱敏逻辑。
- `sdn.models` 是**纯数据**：不 import 任何内部模块，无 IO。
- `tools` → `graph`（client + exceptions + models）；**禁止** import `neo4j`。
- `graph.client` → `app.common.neo4j` + `graph.models` + `graph.exceptions`；仅 `client.py` 处理驱动异常（`_map_neo4j_error`），将其映射为 `GraphError`。
- `common.neo4j` 与具体图库无关（integration-agnostic）：不含标签、Cypher、脱敏逻辑。
- `graph.models` / `graph.cypher` 是**纯数据**：不 import 任何内部模块，无 IO。

**为什么要这样分**：MCP 会把未捕获异常的 `str()` 作为 `isError` 文本回传给模型，而 httpx 错误串里含 URL/状态码。分层 + 脱敏异常层级保证**到达模型的永远是人工撰写的安全消息**。

## 5. 调用规范：Tools → SDNClient → Models

一次工具调用的完整链路：

```
MCP tool 函数 (app/tools/*.py)
  │  1. ctx.request_context.lifespan_context["sdn_client"] 取 SDNClient
  │  2. 校验入参范围（入参错误可安全回显）
  │  3. 调用一个 client 业务方法
  └─▶ SDNClient 业务方法 (app/sdn/client.py)
        │  拥有：端点（settings.sdn.endpoints）、请求体、响应解析
        └─▶ self.request() → self._send()（token 刷新透明处理）
              └─▶ HttpClient._request()（retry / timeout）
                    └─▶ httpx → controller
        ◀── 响应用 Pydantic 模型 model_validate（边界校验），失败抛 SDNError
  ◀── 工具把 model_dump() 包进结构化 dict 返回
```

### Tool 层规范

- 每个工具模块提供 `register(mcp: FastMCP) -> None`，在 `app/tools/__init__.py::register_all` 中登记一行即可生效。
- 工具只做四件事：取 client、校验/预设参数、调**一个** client 方法、转换结果为 dict。**端点 URL、请求体、响应解析一律不允许出现在 tools 层**。
- 返回结构化 dict 信封：`{"ok": bool, "configured": bool, ...result.model_dump()}`，失败为 `{"ok": false, "detail": <安全消息>}`。
- 错误处理**必须两层兜底**（见 `app/tools/system.py` 头部注释）：
  1. `except SDNError as exc:` → `await ctx.error(...)` 记日志 + 返回 `str(exc)`（SDNError 的 `__str__` 只含安全 message）；
  2. `except Exception:` → `logger.exception(...)` + 返回通用 `"Unexpected server error."`。**绝不能让原始异常进入 MCP 错误路径**。
- 通过 lifespan 上下文拿 client：`sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]`（lifespan 由 `server.py::_make_lifespan` 提供，每会话一个实例，退出时 `aclose()`）。
- 未配置时短路：`if not sdn.configured: return {"ok": False, "configured": False, ...}`。
- 新工具按域拆成独立模块（`device_tools`/`link_tools`/`topology_tools`/`perf_tools`/`log_tools`/`alert_tools`），各自 `register(mcp)`，共享校验走 `validation.py`（`page_bounds_detail`/`time_window_detail`/`skeleton_payload`/`unexpected_payload`）。`sdn_tools.py` 仅保留旧 `sdn_alerts` 桩，不再往里加新工具。
- **分页**：工具层统一 1-based `page_num`/`page_size`（上限 `validation.MAX_PAGE_SIZE=100`），client 按端点换算（链路 `page=page_num-1`；设备 `pageNumber`/告警 `pageNum`/日志 `pageNum` 直通）。Spring 信封 `number` 为 0-based，原样回显（无行内注释，语义见本节+测试）。
- **校验分工**：枚举用 `Literal` 工具参数（非法值由 MCP schema 拦截为 `isError=True`）；页码/时间窗/非空 ID 等返回 `{"ok": False, "detail": ...}`。
- 性能/告警/日志的时间窗默认 now-1h→now（client 私有 helper `_default_time_window`/`_resolve_window`/`_perf_params`）。

### Client 层规范

- 业务方法（`health`、`query_alerts` 等）统一走 `self.request(...)`（内部转 `self._send(...)`），**不接触 token、header、httpx 异常之外的任何东西**。
- httpx 异常必须在业务方法边界用 `_map_http_error(exc)` 映射后抛出（`raise ... from exc`），对外只抛 `SDNError` 子类。
- 响应必须经 Pydantic 模型 `model_validate` 校验；`ValidationError/ValueError` → `SDNError("... was malformed.", detail=str(exc))`。
- 端点从 `self._settings.sdn.endpoints` 取（YAML 配置），可带默认值：`endpoints.get("alerts", "/monitor/v2/alert/page")`。

### Models 层规范

- 每个控制器响应在边界用**具名模型**校验（"validate external data" 规则）。
- 控制器 schema 宽且会演进时，对已知字段 typing + `model_config = ConfigDict(extra="allow")` 保留未知字段（见 `SDNAlert`）。
- 命名：响应用 `SDN*Response`，实体用领域名（`Device`、`Topology`），字段 snake_case。
- v1.5 分页端点（设备 §2.2 / 链路 §2.16）用 PEP 695 泛型信封 `PageResponse[T: BaseModel]`（Spring-Data 信封：`content/total_elements/number/...`，`extra="allow"` 保留 `pageable/sort`）。
- **瘦类型规则**：只给 JSON 类型确定的字符串字段 + 嵌套对象结构加类型；数值型/未知类型字段走 `extra="allow"`，live 验证后再提升（避免猜错标量导致整页校验失败）。
- **凭据剥离**：`Device` 用 `@model_validator(mode="before")` 调 `_strip_sensitive` 递归删除 `password`/`community`（含嵌套），凭据永不进入 MCP/模型层；`username` 等业务字段保留。
- 工具输出用 `model_dump(by_alias=True)`，键名还原为控制器词汇（kebab/camel）。

## 6. HttpClient 重试与超时（`app/common/http.py`）

`HttpClient(HttpClientConfig(...))`，三个配置 dataclass：

| 配置 | 默认值 | 说明 |
|---|---|---|
| `HttpClientConfig.timeout` | `30.0` 秒 | 传给 httpx 的整体超时；SDN 层来自 `sdn.timeout`（YAML） |
| `HttpClientConfig.ssl_verify` | `True` | 自签名证书在 YAML 设 `sdn.ssl_verify: false` |
| `HttpClientConfig.transport` | `None` | 注入 `httpx.MockTransport` 做单测的关键接缝 |
| `HttpClientConfig.log_bodies` | `False` | 成功路径记录请求/响应体（DEBUG，已脱敏+截断）；**错误**响应体始终在 WARNING 记录（脱敏），不受此开关影响 |
| `HttpClientConfig.body_log_limit` | `2048` | 单条 body 日志的字符截断上限 |
| `HttpClientConfig.extra_sensitive_keys` | `()` | 内置敏感词之外额外需脱敏的 JSON 键（登录客户端注入 `token_field`） |
| `RetryConfig.max_retries` | `3` | 最多重试 3 次 → 合计 **4 次尝试** |
| `RetryConfig.base_delay` | `1.0` 秒 | 指数退避基数 |
| `RetryConfig.max_delay` | `30.0` 秒 | 单次等待上限 |

**重试判定（`_is_retriable`）：**

- ✅ 重试：`httpx.TransportError`（连接/超时/读错误）、HTTP **429**、HTTP **5xx**。
- ❌ 立即抛出：**其余 4xx 一律不重试**（客户端错误，重试也不会成功；重试 4xx 视为 bug）。
- 注意：重试**不区分 HTTP 方法**——POST/PUT/PATCH/DELETE 在 429/5xx/传输错误下同样重试。对非幂等业务要意识到这一点（控制器的登录/告警端点均按可重试设计）。

**退避策略**：指数上限 `cap = min(base_delay * 2^(attempt-1), max_delay)`，再取 **full jitter** `random.uniform(0, cap)`（AWS 式，避免并发客户端同步重试放大控制器压力）。

**其他约定**：

- `raise_for_status=True` 是默认行为；需要自行检查状态码时显式传 `raise_for_status=False`。
- REST 封装：`get/post/put/patch/delete/head`，全部经 `_request` 统一重试。
- `set_bearer_token(token)`：在**不重建** live client 的前提下热轮换 Authorization 头（SDNClient token 刷新依赖它）。
- 支持 `async with HttpClient(...) as c:` 上下文管理；`close()` 幂等。
- 请求/响应日志（`_request` 内，结构化事件，仅服务端 stderr）：每次尝试发 `http_request`/`http_response`（DEBUG）或 `http_error`（WARNING），`extra` 字段含 `method`/`url`/`attempt`/`status`/`elapsed_ms` 等。URL 仅作结构化字段、header 从不记录；HTTP 错误自动记脱敏+截断响应体（Hybrid 策略），成功路径体需 `log_bodies=True` 且 DEBUG 才记，敏感键（password/token/authorization/…）一律脱敏。**不要**把这些日志转发到模型可读通道（SDN 层已对模型脱敏）。

## 7. Token 重试机制（`app/sdn/client.py`，basic 模式）

三种 `auth_type`（YAML `sdn.auth_type` 选择）：

| auth_type | token 来源 | 首个 token 时机 | 收到 401 |
|---|---|---|---|
| `no-auth` | — | — | 直接报错 |
| `bearer` | `SDN_CONTROLLER_TOKEN`（静态 api-key） | 构造时 | 直接报错（不刷新） |
| `basic` | 登录端点换取 bearer | 启动 `initialize()`，**失败即启动失败**（fail-fast） | 自动重新登录并**只重试一次** |

### basic 模式端到端流程

1. **双客户端结构**：构造时创建 `_http`（数据请求，bearer，初始 token 为空）+ `_login_http`（登录专用，**no-auth**）。登录请求不带 bearer、不走 `_send` 刷新逻辑 → **结构上不可能递归**（登录 401 不会触发刷新流程）。
2. **启动登录**：MCP lifespan 调用 `initialize()` → `_refresh_token(stale_gen=0)` 强制首次登录。默认登录契约：向 `endpoints['login']` **POST** JSON `{username, password, device_id}`（`device_id` 为进程级 UUID；凭证走 body 不走 header），从响应 JSON 的 `token_field`（默认 `access_token`）取 token。
3. **运行期刷新**（`_send`）：数据请求捕获 `HTTPStatusError` 且 `status_code == 401` →
   - `_refresh_token(stale_gen)` 刷新 → 原请求**重试一次**；
   - 第二次再 401 / 任何失败**直接上抛** → **永不死循环**。
4. **并发击穿防护**（`_refresh_token`）：
   - `asyncio.Lock` 串行化刷新；
   - **代际计数器** `_refresh_gen`：调用方在请求前记录 `stale_gen`，刷新后若代际已推进则跳过——N 个并发 401 **只登录一次**（即便新旧 token 字符串相同也能去重，值相等判断做不到这点）；
   - **负缓存**：登录失败记录异常，`LOGIN_FAILURE_COOLDOWN = 5.0` 秒内的后续刷新直接复抛缓存的异常，避免反复冲击宕机/拒证的控制器。
5. **token 落地**：`await self._http.set_bearer_token(new_token)` 热更新 live client 头。

### 适配非标准控制器

- 登录契约不同（method / body / token JSON 路径）时：**只重写 `SDNClient.get_token()`**，其余刷新机制原样复用。
- `basic` 模式启动前校验必备项（`_require_basic_credentials`）：`SDN_CONTROLLER_USERNAME`、`SDN_CONTROLLER_PASSWORD`、`sdn.endpoints.login`，缺一抛 `SDNConfigError`（fail-fast）。

### 错误层级与脱敏（`app/sdn/exceptions.py`）

| 异常 | 触发 |
|---|---|
| `SDNError`（基类） | 兜底 / 响应 malformed |
| `SDNConfigError` | 未配置 base_url；basic 缺凭证/登录端点 |
| `SDNConnectionError` | `ConnectError` / `TimeoutException` / 其他 `TransportError` |
| `SDNAuthError` | HTTP 401/403；登录响应 malformed |
| `SDNNotFoundError` | HTTP 404 |
| `SDNHTTPError` | 其他非成功 HTTP 状态 |

铁律：`__str__` 只返回人工撰写的 `public_message`；原始异常串放 `detail`（**仅服务端日志**，不得回显给模型）。映射集中在 `client.py::_map_http_error`。

## 8. 图库（Neo4j）

与 HTTP 栈完全同构的分层：`app/common/neo4j.py` 通用驱动（integration-agnostic）+ `app/graph/` 集成层（`client.py`/`cypher.py`/`models.py`/`exceptions.py`）。

**配置与骨架模式**：非敏感结构（`database`/`query_timeout`/`max_transaction_retry_time`/`log_params`）在 YAML `neo4j:` 块；连接三件套走 env——`NEO4J_URI`（优先于 YAML `neo4j.uri`）、`NEO4J_USERNAME`、`NEO4J_PASSWORD`（`SecretStr`，`neo4j_username`/`neo4j_password` 键出现在 YAML 会被启动时拒绝）。`NEO4J_URI` 空 = 骨架模式（`configured is False`）。lifespan 启动时 `probe()` 探测但**不 fail-fast**（与 SDN basic 登录不同）：图库宕机只记 WARNING `graph_probe_failed`，SDN 工具不受影响；关闭顺序先 graph 后 sdn。

**`_db` 逻辑库守卫（核心约定）**：同一物理 Neo4j 里用节点属性 `_db` 区分多个逻辑库（常量 `graph.cypher.DB_PROPERTY`）。逻辑库是**数据不是配置**——`Neo4jSettings` 故意没有 `db_tag` 字段，`db_tag` 每次 query 调用现填：

- `run_read(cypher, params, *, db_tag, ...)` 的 `db_tag` 关键字**必填无默认值**；非 None 时注入 `params["_db"]`（覆盖调用方预置值）并要求 Cypher 文本含 `$_db` 过滤；
- `db_tag=None` 必须显式 `allow_cross_db=True`（跨库是审计点，不得隐式发生）；
- 违反守卫抛 `ValueError`——这是开发期编程错误，不映射为 `GraphError`。

**重试交给驱动**：读取走 `session.execute_read` 托管事务，驱动按 `max_transaction_retry_time` 内建重试；**不自实现重试/退避**（对照 HTTP 栈的 `RetryConfig`）。

**生命周期**：与 SDNClient 一致——每会话一个 `GraphClient`（自持 driver），lifespan `finally` 关闭（`aclose()` 幂等）。将来若会话 churn 变高，只需把 `server.py` 的 factory 改成进程级单例 driver。

**networkx 结论：现阶段不引入**（spec-02 §11）。当前需求只是"从一个 Event 取有界子图"，Cypher 变长路径一次查询即可；实例化 networkx 等于建第二份真相（缓存过期 → agent 拿到被人工编辑过的旧流程，是正确性风险）。留门：`GraphFragment` 为序列化中立的节点集+边集，将来确需算法时新增 `app/graph/nx.py::to_digraph(fragment)` 即可，`client.py`/`tools/` 不动。

**错误层级与脱敏**（同 SDN 铁律：`__str__` 只返回人工撰写的安全消息，原始串放 `detail` 仅服务端日志）：`GraphError`（兜底）/`GraphConfigError`（未配置）/`GraphConnectionError`（`ServiceUnavailable`/`SessionExpired`）/`GraphAuthError`（`AuthError`）/`GraphQueryError`（其他 `ClientError`）。映射集中在 `graph.client::_map_neo4j_error`——**`AuthError` 是 `ClientError` 子类，必须先判断**。

**日志**：事件与 `http.py` 对齐——`neo4j_query`/`neo4j_result`（DEBUG）、`neo4j_error`（WARNING），`extra` 含 `query_name`/`db_tag`/`record_count`/`elapsed_ms`/`error_type`。密码永不落日志；`params` 仅 `neo4j.log_params: true` 且 DEBUG 时输出；Cypher 全文不作默认日志字段（`query_name` 短名定位够用）。

## 9. 新 SDN Client / 新工具开发规范

### 9.1 给 SDNClient 增加业务方法（最常见）

三处改动，**不需要改 server.py / settings.py**：

1. **`app/sdn/models.py`** — 加响应模型：
   ```python
   class Device(BaseModel):
       id: str
       name: str
       kind: str | None = None
   ```
2. **`app/sdn/client.py`** — 加业务方法（照抄 `query_alerts` 的骨架）：
   ```python
   async def get_devices(self) -> list[Device]:
       self._require_configured()
       endpoint = self._settings.sdn.endpoints["devices"]
       try:
           resp = await self._send("get", endpoint)   # 必须走 _send，token 刷新透明
       except httpx.HTTPError as exc:
           raise _map_http_error(exc) from exc         # 必须映射，不外抛 httpx 异常
       try:
           return [Device.model_validate(d) for d in resp.json().get("devices", [])]
       except (ValueError, ValidationError) as exc:
           raise SDNError("SDN devices response was malformed.", detail=str(exc)) from exc
   ```
3. **`app/tools/sdn_tools.py`** — 在 `register()` 内加 `@mcp.tool(...)` 函数（照抄 `sdn_alerts`）：
   取 client → 校验入参 → 调一个 client 方法 → 返回 `{"ok": True, "configured": True, **result...}`；
   两层 except 兜底（`SDNError` + `Exception`）。新工具模块则还需在 `app/tools/__init__.py::register_all` 登记一行。

同步：端点写进 `config/sdn_controller.yaml` 的 `sdn.endpoints`；补测试（见 §10）。

> v1.5 已落地的业务方法：`query_devices`/`query_links`/`query_switch_history`/`query_vpn_history`/`query_te_history`/`get_topology`/`query_operation_logs`/`run_command`，均追加在 `SDNClient` 类尾、走 `self.request`。告警走**新方法 `query_alert_page`**（§3.5 多条件契约）+ 新工具 `sdn_device_alerts`（`alert_tools.py`），**旧 `query_alerts`/`sdn_alerts` 保留作框架桩不动**。`run_command`（`cmd_tools.py::sdn_run_command`）对设备下发 CLI 命令，**默认只读**（仅诊断类命令；`allow_write=True` 覆盖）——只读策略属工具层输入校验，client 为透传。

> **占位工具约定（spec-01）**：数据源未接入的工具先钉注册面——按最终签名注册，函数体只做参数校验并返回 `validation.not_implemented_payload(hint)`（`{ok: false, configured: false, detail: "Tool is registered but not implemented yet."}`，`hint` 点名待接入数据源）；占位阶段不加 `ctx` 与两层 except（无 IO、避免不可达分支）。落实现时只替换函数体并按 §5 补两层兜底，**参数名不得再改**。

### 9.2 接入一个全新类型的控制器

- 优先复用 `SDNClient`：鉴权差异用 `auth_type` 三选一覆盖；登录契约差异**只重写 `get_token()`**。
- 确需新 client 类时（如另一种控制协议）：照 `app/sdn/` 建包（`client.py` + `models.py` + `exceptions.py`），复用 `app/common/http.py` 的 `HttpClient`，在 `server.py` lifespan 中增配一个上下文 key，tools 从 lifespan 取用。**禁止**绕过 `HttpClient` 直接用 `httpx`（会丢掉统一重试/超时/可测试性）。

### 9.3 硬性规则清单（DON'T）

- ❌ tools 层出现端点 URL / 请求体 / 响应解析 / `import httpx`。
- ❌ 业务方法绕过 `_send` 直接调 `self._http.get(...)`（丢失 token 刷新）。
- ❌ 对外抛出原始 httpx 异常或未脱敏消息；`except Exception` 裸透传给 MCP。
- ❌ 重试 4xx（429 除外）；自行实现退避循环（用 `RetryConfig`）。
- ❌ 把 secret 写进 `config/sdn_controller.yaml`——`settings.py::_reject_secrets_in_yaml` 会在启动时**拒绝加载**含 `sdn_controller_token/username/password`、`neo4j_username/neo4j_password` 键的 YAML（设计使然，不要绕过）。
- ❌ token/凭证进 URL query 或 path（bearer 走 header，登录凭证走 JSON body）。
- ❌ models 里做 IO / import 内部模块。

## 10. 测试与质量

```bash
uv run pytest            # asyncio_mode=auto；addopts 内置 --cov=app --cov-fail-under=80
uv run ruff check .
uv run mypy
```

- **HTTP mock**：`httpx.MockTransport(handler)` 经 `SDNClient(settings, transport=..., login_transport=...)` 注入——重试、超时、401 刷新全流程无需真实网络（见 `tests/test_http_client.py`、`tests/test_sdn_client.py`）。
- **MCP 集成**：`create_connected_server_and_client_session(mcp)` 内存传输起会话，`conftest.py::make_session` 工厂把 mock transport 与 `build_server(sdn_client_factory=...)` 缝在一起。
- **环境隔离**：`conftest.py` 的 autouse fixture 关闭 dotenv 并清除 `SDN_CONTROLLER_*` / `NEO4J_*` 环境变量，保证 Settings 确定性；需要时用 `monkeypatch.setenv`。
- 测试构造 Settings 的惯例：`make_sdn_settings()`（`retry max_retries=0`、`timeout=1.0`，跑得快且可预测）；图库侧用 `make_graph_settings()` + conftest 的 fake Neo4j driver（记录 cypher/params/database 调用）。
- **Neo4j mock**：fake `AsyncDriver`/`AsyncSession`/`AsyncManagedTransaction` 经 `GraphClient(settings, driver=...)` 注入——`_db` 守卫、错误映射、生命周期全程无需真实图库。
- live 验证（手动，需真实控制器）：`PYTHONPATH=. uv run python scripts/test_sdn_live.py`。该脚本逐步跑全部 v1.5 业务方法、逐步容错并汇总 PASS/FAIL，兼作**瘦类型 schema 探针**——发现字段差异回填 `app/sdn/models.py`。

## 11. 运行与部署

```bash
uv run sdn-mcp                                  # Streamable HTTP，127.0.0.1:8000/mcp
uv run sdn-mcp --host 0.0.0.0 --port 9000
uv run sdn-mcp --transport stdio                # Claude Desktop 等本地客户端
uv run python scripts/test_client.py list-tools --url http://127.0.0.1:8000/mcp
uv run python scripts/test_client.py call-tool --url http://127.0.0.1:8000/mcp --name sdn_health
```

环境变量（`.env`，从 `.env.example` 复制，**勿提交真实值**）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | Streamable HTTP 绑定地址（CLI `--host/--port` 可覆盖） |
| `MCP_LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL；同时驱动 `app.*` 的 JSON 结构化日志（`app/common/logging.py::setup_logging`，`main()` 启动时调用，每行一个 JSON 对象输出到 stderr） |
| `MCP_DNS_REBINDING_PROTECTION` | `false` | 默认关闭以便 Cherry Studio/Cursor 等 Electron 客户端直连；公网/反代部署时设 `true` |
| `SDN_CONTROLLER_BASE_URL` | 空 | 控制器地址；**env 优先于 YAML**；空 = 骨架模式 |
| `SDN_CONTROLLER_USERNAME` / `SDN_CONTROLLER_PASSWORD` | — | basic 模式凭证（`SecretStr`，仅 env/.env） |
| `SDN_CONTROLLER_TOKEN` | — | bearer 模式固定 api-key（`SecretStr`） |
| `NEO4J_URI` | 空 | Neo4j 地址（如 `bolt://host:7687`）；**env 优先于 YAML `neo4j.uri`**；空 = 图库骨架模式 |
| `NEO4J_USERNAME` / `NEO4J_PASSWORD` | — | Neo4j 凭证（`SecretStr`，仅 env/.env；出现在 YAML 会被启动时拒绝） |
| `SDN_CONFIG_FILE` | `config/sdn_controller.yaml` | YAML 路径覆盖（Docker 内置 `/app/config/...`） |

Docker（Makefile 封装 `deploy/docker-compose.yml`，自动带项目根 `.env`）：

```bash
make docker-build | docker-up | docker-stop | docker-restart | docker-ps | docker-logs
```

镜像为多阶段构建：uv builder 安装依赖 → `python:3.13-slim` 运行镜像；secret 只经环境变量注入、绝不烤进镜像；`config/` 以**只读卷**挂载。

## 12. 排错速查

- `uv run sdn-mcp` 报 `ModuleNotFoundError: No module named 'app'`：editable 安装的 `.pth` 在某些 Python 构建上不加载。修复：`rm -rf .venv && uv venv && uv sync`，或 `uv pip install .`，或 `uv run python -m app.server`（见 README「排错」）。
- 启动即 `SDNConfigError`：`auth_type=basic` 但缺 username/password/`endpoints.login`——按提示补齐 `.env` 与 YAML。
- GUI 客户端连不上：确认 `MCP_DNS_REBINDING_PROTECTION=false`（默认）且地址端口正确。
- 排障 SDN 调用错误：`MCP_LOG_LEVEL=DEBUG` 启动后，stderr 可见每次请求的 `http_request`/`http_response`（DEBUG，含 status/elapsed）；HTTP 错误的脱敏响应体在 WARNING 自动输出。需要看成功路径的请求/响应体时，再在 YAML 设 `sdn.http_log_bodies: true`（敏感键已脱敏，DEBUG 才输出）。
- 图库连不上：server **仍会正常启动**（probe 不 fail-fast），启动日志可见 WARNING `graph_probe_failed`（`error_type` 区分 `ServiceUnavailable`/`AuthError` 等）；运行期查询失败记 `neo4j_error`（`query_name`/`db_tag`/`error_type`/`elapsed_ms`）。SOP 工具只回安全消息（如 "SOP graph is unreachable."），日志外不应出现 URI/驱动异常串——出现即脱敏泄漏，按 bug 处理。
