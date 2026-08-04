# Spec 04：命令模板库（intent × vendor）与 `search_command_template`

> 状态：待实施
> 依据：`AGENTS.md`（= `CLAUDE.md`）§5 调用规范、spec-01（占位签名）、spec-03（`Step.action` 词汇）
> 范围：`config/command_templates.yaml` 数据契约与种子数据、`app/templates/` 加载层、`app/tools/template_tools.py::search_command_template`
> 前置：spec-01（占位已注册）。**不依赖 spec-02/03**——本 spec 无任何网络/图库依赖，可独立先行实施。

## Context

SOP 树里的每个 Step 带一个 `action`（机读意图，如 `verify_interface_state`），它是与厂商无关
的抽象。真正要下发到设备的命令依赖厂商（华为 `display interface ... brief`、Cisco
`show interface ... status`）。命令模板库负责这一层映射：

```
SOP Step.action  ──►  search_command_template(action, vendor)  ──►  command 文本
                                                                      │
                                                                      ▼
                                                            sdn_run_command（只读白名单）
```

**为什么用 YAML 而不是图库**：这是一张纯查表（`action × vendor → command`），没有关系、没有
遍历、没有版本回放需求。放 YAML 的收益是：可 review、可 diff、随代码一起版本化、零运行时
依赖、改一行不用动数据库。放图库反而要为一张二维表引入连接、鉴权与故障面。

**"受控检索"的含义**：命令文本只能来自这份人工维护的白名单，LLM 不能自由造命令。工具返回
模板与占位符，**不做渲染**（见 §5）。

## 1. 数据契约（`config/command_templates.yaml`）

一级 key = `action`（与 SOP `Step.action` 完全对齐），二级 key = `vendor`，叶子最少只需
`command`。

```yaml
# Command template library (NON-SECRET, safe to commit).
# Layout: templates.<action>.vendors.<vendor>.command
#   <action> matches the SOP graph's Step.action verbatim (see docs/spec-03).
#   <vendor> is lowercase; `default` is the fallback used when the requested
#   vendor has no entry. Placeholders are `{name}` and are NOT rendered by the
#   server — search_command_template returns them for the caller to fill.
# Override the file path with COMMAND_TEMPLATE_FILE.

templates:
  verify_interface_state:
    name: 检查接口状态
    observation: oper_state
    vendors:
      huawei:  { command: "display interface {interface} brief" }
      h3c:     { command: "display interface {interface} brief" }
      cisco:   { command: "show interface {interface} status" }
      default: { command: "show interface {interface}" }
```

字段规则：

| 层级 | 键 | 必填 | 说明 |
|---|---|---|---|
| action | `name` | 否 | 人读名称，参与 keyword 检索 |
| action | `observation` | 否 | 该 action 通常要提取的回显字段，与 SOP `Step.observation` 呼应 |
| action | `vendors` | **是** | 至少一个 vendor 条目 |
| vendor | `command` | **是** | 非空字符串 |
| vendor | 其它键 | 否 | 如 `notes`、`parser`、`requires_enable`，**原样回显** |

"其它键原样回显"是刻意的：模板库会随实战演进（加解析提示、加注意事项），加载层不该因为
出现未知键就报错，工具也应把它们透传给 agent。

## 2. 种子数据（首批 9 个 action）

action 命名与 SOP 图的 `Step.action` 词汇对齐（`verb_object` 蛇形小写）。vendors 一律提供
`huawei` / `h3c` / `cisco` / `default` 四条，命令文本按各厂商常用形式给出，**实施时需按现网
设备型号复核**（这是唯一需要现场校准的部分）。

```yaml
  check_interface_errors:
    name: 检查接口错包与 CRC
    observation: error_counters
    vendors:
      huawei:  { command: "display interface {interface}" }
      h3c:     { command: "display counters inbound interface {interface}" }
      cisco:   { command: "show interface {interface} counters errors" }
      default: { command: "show interface {interface}" }

  check_optical_power:
    name: 检查光模块收发光功率
    observation: rx_power
    vendors:
      huawei:  { command: "display transceiver interface {interface} verbose" }
      h3c:     { command: "display transceiver diagnosis interface {interface}" }
      cisco:   { command: "show interface {interface} transceiver detail" }
      default: { command: "show transceiver {interface}" }

  check_bgp_neighbor:
    name: 检查 BGP 邻居状态
    observation: bgp_state
    vendors:
      huawei:  { command: "display bgp peer {neighbor_ip} verbose" }
      h3c:     { command: "display bgp peer ipv4 {neighbor_ip} verbose" }
      cisco:   { command: "show bgp ipv4 unicast neighbors {neighbor_ip}" }
      default: { command: "show bgp neighbor {neighbor_ip}" }

  check_isis_neighbor:
    name: 检查 ISIS 邻居状态
    observation: isis_state
    vendors:
      huawei:  { command: "display isis peer verbose" }
      h3c:     { command: "display isis peer verbose" }
      cisco:   { command: "show isis neighbors detail" }
      default: { command: "show isis neighbor" }

  check_route:
    name: 检查路由表项
    observation: next_hop
    vendors:
      huawei:  { command: "display ip routing-table {prefix}" }
      h3c:     { command: "display ip routing-table {prefix}" }
      cisco:   { command: "show ip route {prefix}" }
      default: { command: "show ip route {prefix}" }

  check_cpu_memory:
    name: 检查 CPU 与内存占用
    observation: cpu_usage
    vendors:
      huawei:  { command: "display cpu-usage" }
      h3c:     { command: "display cpu-usage summary" }
      cisco:   { command: "show processes cpu sorted" }
      default: { command: "show cpu" }

  check_device_log:
    name: 检查设备日志
    observation: log_entries
    vendors:
      huawei:  { command: "display logbuffer" }
      h3c:     { command: "display logbuffer" }
      cisco:   { command: "show logging last 100" }
      default: { command: "show log" }

  check_te_tunnel:
    name: 检查 TE 隧道状态
    observation: tunnel_state
    vendors:
      huawei:  { command: "display mpls te tunnel-interface {tunnel}" }
      h3c:     { command: "display mpls te tunnel-interface {tunnel}" }
      cisco:   { command: "show mpls traffic-eng tunnels {tunnel}" }
      default: { command: "show mpls te tunnel {tunnel}" }
```

**所有种子命令都是只读诊断命令**（`display` / `show` 开头），因此能直接通过
`sdn_run_command` 的默认只读白名单（`cmd_tools._READONLY_PREFIXES`）。往库里加配置类命令
时要意识到：`sdn_run_command` 会拒绝执行，除非调用方显式 `allow_write=True`。建议**模板库
只放只读命令**，配置变更走人工流程。

## 3. 加载层（`app/templates/` 新包）

```
app/templates/
├── __init__.py       # 导出 CommandTemplateRegistry / TemplateError / 模型
├── exceptions.py     # TemplateError
├── models.py         # 纯数据 Pydantic
└── registry.py       # 加载 / 校验 / 索引 / 查询
```

放独立包而不是塞进 `app/common/`：`common/` 是"与集成无关的通用基础设施"（HTTP、日志、
Neo4j 驱动），而命令模板库是**领域数据**（有 action/vendor 语义）。它与 `app/sdn/`、
`app/graph/` 同级，都是"某个数据源的集成层"，只不过它的数据源是本地文件。

### 3.1 `models.py`

```python
class CommandTemplate(BaseModel):
    """One (action, vendor) command template."""

    model_config = ConfigDict(extra="allow")   # keep notes/parser/... as authored

    action: str
    vendor: str
    command: str
    name: str = ""
    observation: str = ""
    # Set when the requested vendor had no entry and the `default` one was used.
    fallback: str | None = None
    # Placeholder names extracted from `command`, e.g. ["interface"].
    placeholders: list[str] = Field(default_factory=list)


class ActionSummary(BaseModel):
    """One action with the vendors it covers (used by list-style responses)."""

    action: str
    name: str = ""
    observation: str = ""
    vendors: list[str] = Field(default_factory=list)
```

`CommandTemplate` 是**扁平**的（`action` 和 `vendor` 都在同一层），YAML 是嵌套的——加载时
展平。理由：工具返回的每一条都自带完整坐标，agent 不需要靠外层上下文推断这条命令属于哪个
action/vendor。

### 3.2 `exceptions.py`

```python
class TemplateError(Exception):
    """The command template library is missing or malformed."""
```

**不需要** `public_message` / `detail` 双层结构（对比 `SDNError` / `GraphError`）：这里的
异常只可能来自本地配置文件损坏，消息由本项目自己撰写、不含外部系统的 URL 或凭据，天然安全。
唯一要注意的是**不要把文件绝对路径拼进给模型的消息里**（暴露部署路径），路径进日志即可。

### 3.3 `registry.py`

```python
"""Command template library: a local YAML lookup table (action x vendor).

The library is human-curated and version-controlled: the server never invents a
command, it only serves what is written in ``config/command_templates.yaml``.
Loaded lazily on first use and cached for the process lifetime — the file is a
static deployment artifact, so there is no reload path (restart to pick up edits).
"""

DEFAULT_TEMPLATE_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "command_templates.yaml"

DEFAULT_VENDOR: Final = "default"
MAX_TEMPLATE_RESULTS: Final = 50

# Vendor spellings normalized onto the canonical keys used in the YAML.
_VENDOR_ALIASES: Final[dict[str, str]] = {
    "hw": "huawei",
    "huawei": "huawei",
    "vrp": "huawei",
    "h3c": "h3c",
    "hpe": "h3c",
    "comware": "h3c",
    "cisco": "cisco",
    "ios": "cisco",
    "iosxr": "cisco",
    "ios-xr": "cisco",
    "nxos": "cisco",
    "zte": "zte",
    "ruijie": "ruijie",
}

_PLACEHOLDER_RE: Final = re.compile(r"\{(\w+)\}")


def normalize_vendor(vendor: str) -> str:
    """Map a vendor spelling onto its canonical key (unknown values pass through)."""
    key = vendor.strip().lower().replace(" ", "")
    return _VENDOR_ALIASES.get(key, key)
```

`DEFAULT_TEMPLATE_FILE` 按模块位置解析（与 `settings.DEFAULT_CONFIG_FILE` 同法），
让默认路径不受进程 CWD 影响；`COMMAND_TEMPLATE_FILE` 环境变量可覆盖（Docker 内指向
`/app/config/command_templates.yaml`，与 `SDN_CONFIG_FILE` 同一套只读卷挂载）。

Registry 接口：

```python
class CommandTemplateRegistry:
    """Indexed, validated view of the command template YAML."""

    def __init__(self, templates: dict[str, dict[str, Any]]) -> None: ...

    @classmethod
    def load(cls, path: Path | None = None) -> CommandTemplateRegistry:
        """Parse and validate the YAML. Raises TemplateError on any problem."""

    def get(self, action: str, vendor: str) -> CommandTemplate | None:
        """Exact (action, vendor) lookup, falling back to the `default` vendor."""

    def get_all_vendors(self, action: str) -> list[CommandTemplate]:
        """Every vendor variant of one action ([] when the action is unknown)."""

    def search(self, keyword: str | None = None, vendor: str | None = None,
               limit: int = MAX_TEMPLATE_RESULTS) -> list[ActionSummary]:
        """List actions matching a keyword and/or covered by a vendor."""

    @property
    def actions(self) -> list[str]: ...


def get_registry() -> CommandTemplateRegistry:
    """Return the process-wide registry, loading it on first use."""
```

进程级缓存实现（模块级单例 + 显式清理接口供测试用）：

```python
_registry: CommandTemplateRegistry | None = None


def get_registry() -> CommandTemplateRegistry:
    global _registry
    if _registry is None:
        _registry = CommandTemplateRegistry.load()
    return _registry


def reset_registry() -> None:
    """Drop the cached registry (tests only)."""
    global _registry
    _registry = None
```

`reset_registry` 的 docstring 明确写 "tests only"——生产不提供热重载，改模板需重启，理由是
模板库是部署产物，运行期热改会让"某次排障用的是哪版命令"不可追溯。

### 3.4 加载期校验（fail-fast）

`load()` 里逐项校验，任何问题抛 `TemplateError`，消息指出 action/vendor 但**不含绝对路径**：

1. 文件不存在 → `TemplateError("command template library file is missing")`（路径进日志）；
2. YAML 解析失败 / 顶层不是 mapping / 缺 `templates` 键 → `TemplateError`；
3. 某 action 的值不是 mapping、或缺 `vendors`、或 `vendors` 为空 →
   `TemplateError(f"action '{action}' has no vendors")`；
4. 某 vendor 的值不是 mapping、或缺 `command`、或 `command` 非字符串/去空后为空 →
   `TemplateError(f"action '{action}' vendor '{vendor}' has an empty command")`；
5. vendor 名归一后**冲突**（如同时写 `hw` 与 `huawei`）→ `TemplateError`，避免"哪条生效"
   取决于 dict 顺序这种隐蔽 bug。

在 `main()` 启动时**预热一次** `get_registry()`（放在 `setup_logging` 之后、`mcp.run` 之前），
让坏配置在启动即暴露，而不是等第一次工具调用才报错：

```python
    # Fail fast on a malformed template library instead of surfacing it as a tool
    # error on the first agent call.
    get_registry()
```

预热失败要不要阻断启动？——**阻断**。与 SDN basic 登录同理：模板库是本地静态文件，损坏是
部署错误，让它在启动时可见远比运行期半残更好。（图库不同：图库是远程依赖，会因网络抖动
不可用，所以 spec-02 选择不阻断。）

### 3.5 占位符提取

```python
def _placeholders(command: str) -> list[str]:
    """Extract `{name}` placeholder names, preserving first-seen order."""
    seen: dict[str, None] = {}
    for match in _PLACEHOLDER_RE.finditer(command):
        seen.setdefault(match.group(1), None)
    return list(seen)
```

用 `dict` 而非 `set` 去重以保持出现顺序（顺序对 agent 填参有提示价值）。

## 4. Tool（`app/tools/template_tools.py`）

替换 spec-01 的占位函数体。参数名与 spec-01 一致（`action` / `vendor` / `keyword`）。

```python
def register(mcp: FastMCP) -> None:
    """Register the command template lookup tool."""

    @mcp.tool(
        name="search_command_template",
        description=(
            "Look up the device CLI command for an abstract action (intent) on a "
            "given vendor. Feed it a SOP step's 'action' plus the device vendor. "
            "action+vendor returns one template; action alone returns every vendor "
            "variant; vendor or keyword alone lists matching actions; no argument "
            "lists all actions. Vendor spellings are normalized (hw/vrp->huawei, "
            "hpe/comware->h3c, ios/nxos->cisco) and fall back to the 'default' "
            "entry. Placeholders like {interface} are returned in 'placeholders' "
            "for the caller to fill — the server does not render them. Commands "
            "come from a curated library only; nothing is generated. Returns "
            "{ok, mode, template|templates|actions}."
        ),
    )
    async def search_command_template(
        ctx: Context[Any, Any, Any],
        action: str | None = None,
        vendor: str | None = None,
        keyword: str | None = None,
    ) -> dict[str, object]:
```

### 4.1 四种检索模式

```python
        try:
            registry = get_registry()

            # 1. action + vendor -> exactly one template (O(1))
            if action and vendor:
                template = registry.get(action, vendor)
                if template is None:
                    return _unknown_action_payload(registry, action, vendor)
                return {"ok": True, "mode": "template",
                        "template": template.model_dump()}

            # 2. action only -> every vendor variant
            if action:
                templates = registry.get_all_vendors(action)
                if not templates:
                    return _unknown_action_payload(registry, action, None)
                return {"ok": True, "mode": "templates",
                        "action": action,
                        "templates": [t.model_dump() for t in templates]}

            # 3/4. vendor and/or keyword -> action list; no argument -> all actions
            summaries = registry.search(keyword=keyword, vendor=vendor)
            return {"ok": True, "mode": "actions",
                    "actions": [s.model_dump() for s in summaries],
                    "total": len(registry.actions),
                    "truncated": len(summaries) >= MAX_TEMPLATE_RESULTS}
        except TemplateError as exc:
            await ctx.error(f"search_command_template failed: {exc}")
            return {"ok": False, "detail": str(exc)}
        except Exception:
            logger.exception("search_command_template unexpected failure")
            return unexpected_payload()
```

两层 except 保留（§5 铁律）：`TemplateError` 的消息是本项目撰写的、可安全回显；其余走
`unexpected_payload()`。

**无 `configured` 字段**：本地文件不存在"未配置"状态——要么加载成功，要么启动就失败了。
其它工具的 `configured` 表达"外部系统是否接好"，这里没有外部系统，加上反而误导。

### 4.2 未知 action 的自纠信封

```python
def _unknown_action_payload(
    registry: CommandTemplateRegistry, action: str, vendor: str | None
) -> dict[str, object]:
    """Unknown action: tell the model what IS available so it can self-correct."""
    available = registry.actions[:MAX_TEMPLATE_RESULTS]
    detail = f"unknown action '{action}'"
    if vendor:
        detail += f" (vendor '{normalize_vendor(vendor)}')"
    return {
        "ok": False,
        "detail": detail,
        "available_actions": available,
        "truncated": len(registry.actions) > len(available),
    }
```

回显可用 action 列表是刻意的：SOP 图与模板库由不同的人维护，`action` 词汇必然出现漂移
（SOP 写了 `check_intf_err`，模板库里是 `check_interface_errors`）。让模型看到候选，它能
自己选最接近的重试一次，而不是把"查不到命令"当成排障终点。

`available_actions` 有界（`MAX_TEMPLATE_RESULTS = 50`），避免库变大后每次错误都灌一大坨
上下文。

### 4.3 vendor 回退语义

`registry.get(action, vendor)` 的回退顺序：

1. 归一后的 vendor 精确命中 → `fallback is None`；
2. 未命中但该 action 有 `default` → 返回 default 条目，`fallback = "default"`，
   `vendor` 字段仍回显**请求的归一 vendor**（让 agent 知道"你要的 X 没有，给你通用版"）；
3. 两者都没有 → `None`，走 `_unknown_action_payload`（区分不了"action 不存在"与
   "action 存在但该 vendor 无条目且无 default"——后者在 detail 里补一句
   `f"no template for vendor '{v}' and no default entry"`）。

回退而非报错的理由：`default` 通常是通用 `show` 语法，在多数设备上能跑；给个可试的命令比
直接失败对排障更有用，而 `fallback` 标记保证 agent 知道这是降级结果、需要留意语法差异。

## 5. 不做渲染（设计决策）

工具返回 `command`（含 `{interface}` 占位符）+ `placeholders` 列表，**不接受参数、不做
字符串替换**。理由：

1. **注入面**：渲染就意味着把模型给的字符串拼进将要在设备上执行的命令。`{interface}` 填成
   `GE0/0/1 | delete flash:` 这类内容会绕过 `sdn_run_command` 的首 token 白名单
   （首 token 仍是 `display`）。不渲染 = 这条攻击路径不存在。
2. **职责清晰**：填参需要设备上下文（接口真名、邻居 IP），那是 agent 结合
   `sdn_device_by_name` / `sdn_link_info` 的结果才知道的，模板库无从得知。
3. **可审计**：agent 拼出的最终命令会完整出现在 `sdn_run_command` 的入参里，日志中一目了然。

将来若确实要加渲染（例如为了减少一轮交互），必须同时满足：占位符值走**严格白名单正则**
（如接口名 `^[A-Za-z0-9/._-]+$`）、拒绝一切 shell/CLI 元字符（`|`、`;`、换行、反引号）、
渲染后仍过 `sdn_run_command` 的只读检查。把这三条写进 docstring 备查。

## 6. 不进 lifespan（设计决策）

其它客户端（`SDNClient`、`GraphClient`）通过 lifespan 注入，因为它们有**连接生命周期**
（建连、token 刷新、关闭）和**凭据**。命令模板库没有：它是进程级只读纯数据，无连接、无
secret、无需清理。放进 lifespan 只会让每个会话重复持有同一份不可变数据，并把一个纯函数式
查表伪装成有状态资源。

因此 `template_tools.py` 直接 `from app.templates import get_registry` 调用。这不违反 §4 的
导入方向规则——规则约束的是"tools 不得绕过集成层直接碰传输库（httpx / neo4j）"，
`app/templates` **就是**这个数据源的集成层，tools 依赖它完全正确。

## 7. 测试

新增 `tests/test_command_templates.py`（registry 层，纯单元，不需要 MCP 会话）：

1. **加载与索引**：写一个 tmp YAML（2 个 action × 3 个 vendor）→ `actions` 顺序稳定、
   `get()` 命中、`command` 正确。
2. **vendor 归一**：`get("verify_interface_state", "HW")` / `"vrp"` / `" Huawei "` 三种写法
   都命中 `huawei` 条目。
3. **default 回退**：请求 `zte`（YAML 里没有）→ 返回 default 条目，`fallback == "default"`，
   `vendor == "zte"`。
4. **无 default 且 vendor 缺失** → `get()` 返回 `None`。
5. **占位符提取**：`"display interface {interface} brief"` → `["interface"]`；
   同名占位符出现两次只保留一个；顺序按首次出现
   （`"{a} {b} {a}"` → `["a", "b"]`）。
6. **extra 键保留**：叶子写 `notes: "仅 V8 支持"` → `model_dump()` 里能取到 `notes`。
7. **加载期校验，五条各一个用例**：文件缺失 / YAML 非 mapping / 缺 `vendors` /
   `command` 为空串 / vendor 别名冲突（同时写 `hw` 与 `huawei`）→ 均抛 `TemplateError`，
   且**断言异常消息不含文件绝对路径**。
8. **`search()`**：keyword 匹配 `action` 与 `name` 两个字段（中文 `name` 也要能命中，
   如 keyword="光模块"）；`vendor` 过滤只返回覆盖该 vendor 的 action；两者组合是**与**关系；
   `limit` 生效。
9. **路径覆盖**：`monkeypatch.setenv("COMMAND_TEMPLATE_FILE", str(tmp_yaml))` +
   `reset_registry()` → `get_registry()` 读的是 tmp 文件（并用 autouse fixture 在每个测试后
   `reset_registry()`，防止进程级缓存跨测试污染）。

新增 `tests/test_template_tools.py`（工具层，走 MCP 会话）：

1. 四种模式各一个用例，断言 `mode` 分别为 `"template"` / `"templates"` / `"actions"` /
   `"actions"`（无参）。
2. 未知 action → `ok is False`、detail 含该 action 名、`available_actions` 非空且有界。
3. action 存在但 vendor 无条目且无 default → detail 里出现 "no default entry" 提示。
4. `TemplateError` 路径：monkeypatch `get_registry` 抛 `TemplateError` → `ok is False` 且
   detail 是该异常消息。
5. 非 `TemplateError` 异常 → `unexpected_payload()`，detail == `"Unexpected server error."`。
6. `list_tools` 里 description 提到"不渲染占位符"与"仅来自 curated 库"（这两句是安全契约的
   对外声明，不能被后续编辑弄丢）。

新增 `tests/test_sop_template_alignment.py`（**跨 spec 一致性护栏，一个用例**）：

断言 spec-03 的 SOP 示例词汇与模板库一级 key 对齐——具体做法是把种子 YAML 里的 action 集合
与一份"SOP 已用 action"清单对比。由于 SOP 数据在图库里、测试拿不到，退化为**静态检查**：
断言种子 YAML 的每个 action 名满足 `^[a-z][a-z0-9_]*$`（蛇形小写，与 `Step.action` 约定一致）
且 `vendors` 至少含 `default`。理由：`default` 保证任何 vendor 都能拿到一个可试命令，是
§4.3 回退语义的前提。

## 8. 验收

- `uv run pytest` 全绿，覆盖率 ≥ 80%。
- `uv run ruff check .`、`uv run mypy` 干净。
- 故意把 `config/command_templates.yaml` 里某条 `command` 改成空串 → `uv run sdn-mcp`
  **启动即失败**并指出 action/vendor（验证 §3.4 的 fail-fast 与预热）。改回后正常启动。
- `uv run python scripts/test_client.py call-tool --name search_command_template` 无参调用
  → 返回 9 个 action 清单。
- 闭环手工验证：`search_sop` 返回的任一 step 的 `action` 能在本库查到命令（若查不到，
  说明两侧词汇漂移，按 §4.2 的 `available_actions` 提示对齐 SOP 数据或补模板）。
- Docker：`deploy/` 的 `config/` 只读卷已挂载整个目录，新文件自动可见；确认镜像内
  `COMMAND_TEMPLATE_FILE` 未设时默认路径解析正确（`/app/config/command_templates.yaml`）。
- **文档同步**：`AGENTS.md` 与 `CLAUDE.md`
  - §3 目录骨架加 `config/command_templates.yaml`、`app/templates/`、
    `app/tools/template_tools.py`；
  - 新增一节「命令模板库」：数据契约、vendor 归一与 default 回退、**不渲染占位符**的安全
    理由、加载 fail-fast 与"改模板需重启"、为何不进 lifespan；
  - §10 环境变量表加 `COMMAND_TEMPLATE_FILE`（默认 `config/command_templates.yaml`）；
  - §8.3 DON'T 清单加一条：不得在服务端渲染命令占位符 / 不得让模型自由生成设备命令。
