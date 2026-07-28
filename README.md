# SDN MCP Template

一个可复用、可部署的 **MCP (Model Context Protocol) server 模板**，让 AI agent
通过工具（tools）操作 SDN 网络控制器。基于官方 `mcp` Python SDK（v1 `FastMCP`），
主传输为 **Streamable HTTP**，同时支持 **stdio**（便于 Claude Desktop 本地联调）。

本仓库交付**骨架**：MCP server、测试 client、通用 HTTP 客户端、SDN 对接层（结构完整、
端点打桩）、配置管理、测试套件与 Docker 部署。接入真实 SDN 控制器时，**只需 3 处改动**
（见[新增一个 SDN 工具](#新增一个-sdn-工具)）。

---

## 功能特性

- **MCP server**（`app/server.py`）：FastMCP + Streamable HTTP（`/mcp`），可选 stdio。
- **测试 client**（`scripts/test_client.py`）：`list-tools` / `call-tool` 命令行。
- **通用 HTTP 客户端**（`app/common/http.py`）：重试（仅 429/5xx/传输错误）、鉴权
  （bearer/basic）、REST 方法封装、可注入 transport（便于单测）。
- **SDN 对接层**（`app/sdn/`）：基于通用客户端，把 httpx 错误映射为安全的 `SDNError`
  层级（公开 message 不含 URL/状态码）。
- **配置**（`app/settings.py`）：YAML 存非敏感结构、`.env`/环境变量存 secret（`SecretStr`）。
- **工具**（`app/tools/`）：`ping`（验证 server）、`sdn_health`（验证 SDN 接缝）。
- **测试**：38+ 用例，覆盖率 ≥96%，含内存传输（无需真起 HTTP）。
- **部署**（`deploy/`）：多阶段 Dockerfile + docker-compose。

## 架构

```
┌──────────────┐   Streamable HTTP   ┌──────────────────────────────────────┐
│  AI agent /  │ ──────────────────▶ │  FastMCP server  (app/server.py)      │
│ test_client  │   /mcp endpoint     │   ├─ lifespan owns SDNClient           │
└──────────────┘ ◀────────────────── │   ├─ tools: ping, sdn_health, ...     │
                 JSON-RPC responses  │   └─ app/sdn/client.py                 │
                                     │        └─ app/common/http.py (retry)   │
                                     │              └─ httpx ──▶ SDN controller │
                                     └──────────────────────────────────────┘
```

## 前置要求

- Python ≥ 3.13
- [uv](https://docs.astral.sh/uv/)（包管理器）

## 安装

```bash
uv sync          # 安装运行依赖
uv sync          # dev 组（pytest/ruff/mypy）默认随 uv sync 安装
```

## 配置

配置分两部分：

1. **非敏感结构** —— `config/sdn_controller.yaml`（可提交、可版本管理）：
   ```yaml
   sdn:
     base_url: ""              # 留空 = 骨架模式（server 正常启动）
     auth_type: "no-auth"      # no-auth | bearer | basic
     timeout: 30.0
     retry: { max_retries: 3, base_delay: 1.0, max_delay: 30.0 }
     endpoints:
       health: "/"
       devices: "/devices"
       topology: "/topology"
   ```

2. **secret 与运行参数** —— `.env`（从 `.env.example` 复制，**切勿提交真实值**）：
   ```bash
   cp .env.example .env
   ```
   ```dotenv
   MCP_HOST=127.0.0.1
   MCP_PORT=8000
   MCP_LOG_LEVEL=INFO
   SDN_TOKEN=...        # bearer token（auth_type=bearer 时）
   SDN_USERNAME=...     # basic auth 用户名
   SDN_PASSWORD=...     # basic auth 密码
   ```

> **骨架模式**：`base_url` 留空时 server 照常启动，`sdn_health` 返回
> `{ok: false, configured: false}`。配置控制器地址与凭证后即可调用真实端点。

## 运行 server

```bash
# Streamable HTTP（默认 127.0.0.1:8000，端点 /mcp）
uv run sdn-mcp
uv run sdn-mcp --host 0.0.0.0 --port 9000

# stdio（Claude Desktop 等本地客户端）
uv run sdn-mcp --transport stdio
```

## 运行测试 client

另开一个终端，server 已启动：

```bash
uv run python scripts/test_client.py list-tools --url http://127.0.0.1:8000/mcp
uv run python scripts/test_client.py call-tool --url http://127.0.0.1:8000/mcp --name ping
uv run python scripts/test_client.py call-tool --url http://127.0.0.1:8000/mcp \
    --name ping --args-json '{"message": "hi"}'
uv run python scripts/test_client.py call-tool --url http://127.0.0.1:8000/mcp --name sdn_health
```

预期：`list-tools` 列出 `ping`、`sdn_health`；`ping` 返回 `pong: ...`；
`sdn_health`（未配置）返回 `{ok: false, configured: false}`。

## 项目结构

```
sdn-mcp-template/
├── app/                         # 主包
│   ├── server.py                #   FastMCP 工厂 + lifespan + CLI 入口
│   ├── settings.py              #   pydantic-settings（YAML + env 合并）
│   ├── common/http.py           #   通用 HTTP 客户端（retry/auth/方法封装）
│   ├── sdn/                     #   SDN 集成
│   │   ├── client.py            #     基于 HttpClient，错误映射为 SDNError
│   │   ├── models.py            #     Pydantic 响应模型
│   │   └── exceptions.py        #     SDNError 层级（安全 message）
│   └── tools/                   #   MCP 工具
│       ├── system.py            #     ping, sdn_health
│       └── sdn_tools.py         #     SDN 查询工具（接入处）
├── config/sdn_controller.yaml   # SDN 非敏感配置
├── scripts/test_client.py       # 测试 MCP client
├── tests/                       # 测试套件（覆盖率 ≥96%）
└── deploy/                      # Dockerfile + docker-compose
```

## 新增一个 SDN 工具

接入真实控制器时，**只需 3 处改动，无需改 server/config**：

1. **`app/sdn/models.py`** —— 加响应模型：
   ```python
   class Device(BaseModel):
       id: str
       name: str
       kind: str | None = None
       status: str | None = None
   ```

2. **`app/sdn/client.py`** —— 加方法（基于 `settings.sdn.endpoints`）：
   ```python
   async def get_devices(self) -> list[Device]:
       self._require_configured()
       endpoint = self._settings.sdn.endpoints["devices"]
       try:
           resp = await self._http.get(endpoint)
       except httpx.HTTPError as exc:
           raise _map_http_error(exc) from exc
       data = resp.json().get("devices", [])
       return [Device.model_validate(d) for d in data]
   ```

3. **`app/tools/sdn_tools.py`** —— 加工具（模式同 `sdn_health`）：
   ```python
   from mcp.server.fastmcp import Context, FastMCP
   from app.sdn import SDNClient, SDNError

   def register(mcp: FastMCP) -> None:
       @mcp.tool(description="List all devices known to the SDN controller.")
       async def list_devices(ctx: Context) -> dict:
           sdn: SDNClient = ctx.request_context.lifespan_context["sdn_client"]
           try:
               return {"devices": [d.model_dump() for d in await sdn.get_devices()]}
           except SDNError as exc:
               await ctx.error(f"list_devices failed: {exc}")
               return {"devices": [], "error": str(exc)}
   ```

`register_all`（`app/tools/__init__.py`）已调用 `sdn_tools.register`，新工具定义后即生效。

## 测试

```bash
uv run pytest                     # 全套测试 + 覆盖率（≥80% 门槛）
uv run ruff check .               # lint
uv run mypy app                   # 类型检查
```

测试使用 MCP 的内存传输（`create_connected_server_and_client_session`），
无需真实 HTTP 或 SDN 控制器；HTTP/SDN 逻辑用 `httpx.MockTransport` 验证。

## Docker 部署

```bash
cp .env.example .env   # 填入 SDN 凭证
docker compose -f deploy/docker-compose.yml up --build
# 访问 http://localhost:8000/mcp
```

镜像为多阶段构建（uv 安装 → slim 运行镜像），secret 经环境变量注入、绝不烤进镜像，
`config/` 以只读卷挂载便于不改镜像调整配置。

## 安全要点

- **secret 隔离**：token/password 仅走 `.env`/环境变量（`SecretStr`），YAML 只存非敏感结构。
- **错误脱敏**：MCP 会把未捕获异常的 `str()` 当作 `isError` 文本回传模型（httpx 错误串含
  URL/状态码）。本模板在工具层捕获所有 `SDNError` 返回结构化 dict，且 `SDNError.__str__`
  只暴露安全 message，原始 detail 仅记录在服务端日志。
- **token 不入 URL**：bearer/basic 仅走 HTTP header，不进 query/path，不记录 request headers。

## 技术说明

本模板基于**已安装的 `mcp==1.28.1`（v1 `FastMCP` API）**。上游 `main` 分支已有 v2
预发布 API（`MCPServer`/`Client`），二者不兼容；升级 SDK 前请先核对 API 变更。

## 排错

**`uv run sdn-mcp` 报 `ModuleNotFoundError: No module named 'app'`**

uv 默认以 editable 方式安装本项目（写一个把项目根加入 `sys.path` 的 `.pth`）。在某些
Python 构建（如 conda 提供的）上 `site.py` 偶发不加载该 `.pth`，导致控制台脚本找不到包。
任意以下方式可恢复：

```bash
# 方式 1：重建 venv（最常见、最简单）
rm -rf .venv && uv venv && uv sync

# 方式 2：改用非 editable 安装（把 app/ 物理拷进 site-packages，最稳）
uv pip install .

# 方式 3：用模块入口（依赖 CWD 为项目根）
uv run python -m app.server
```

这不影响代码本身——`ruff`/`mypy`/`pytest` 都正常；只是该环境下的 editable `.pth` 加载问题。

