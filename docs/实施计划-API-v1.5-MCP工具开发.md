# 实施计划：骨干网操作系统 API v1.5 MCP 工具开发（merge-friendly 版）

> 状态：已确认，实施中
> 依据：`docs/骨干网操作系统API接口规范v1.5-20260707.docx`、`CLAUDE.md`
> 范围：§2.2 设备信息、§2.16 链路分页、§3.2 设备性能统计、§3.3 VPN 性能统计、§3.4 TE 隧道性能统计、§3.5 告警分页、§3.7 拓扑、§3.12 系统操作日志
> 粒度：新工具文件按域拆 **7 个**（已确认）

## Context

仓库是 MCP server 骨架，SDN 层现仅有 `health` 和一个按猜测契约写的桩 `query_alerts`。本次按
规范 v1.5 实现 8 类工具。**首要约束（用户明确）**：便于后续 merge 上游 fork 的框架新代码——
对框架自带文件（`client.py`/`sdn_tools.py`/`system.py`/`models.py`/`tools/__init__.py`）尽量
"追加或不动"，新内容尽量落新文件；新增业务方法追加到 `SDNClient` 类尾；新工具=新文件 +
`register_all` 里一行。**告警走全新一套，不修改旧 `query_alerts`/`sdn_alerts` 契约，影响最小。**

## 规划前已验证（只读 spike）

- 基线绿：`uv run mypy` 干净；`uv run pytest` 90 通过、99.55% 覆盖率。
- `CLAUDE.md` 与 `AGENTS.md` 逐字节相同 → 文档改动两文件同步。
- httpx 0.28.1：点号键原样传递；逗号→`%2C`、空格→`+`（Spring 可解码）；**`None` 值会序列化为空串 → client 必须过滤 `None`**。
- pydantic 2.13.4：泛型 `PageResponse[T]` + `extra="allow"` 可用；`model_dump(by_alias=True)` 还原 kebab 别名且保留 extras；mode="before" 校验器可在校验前剥敏。
- `query_alerts`/`sdn_alerts` 现仅 `app/tools/sdn_tools.py` 一处生产调用——但本计划**不改它们**。

## 总体设计决策

1. **分页**：工具层统一 1-based `page_num`/`page_size`（上限 100）；client 按端点换算：

   | 端点 | 线上参数 | 换算 |
   |---|---|---|
   | §2.2 设备 | `pageNumber`/`pageSize`（POST query） | 直通 |
   | §2.16 链路 | `page`/`size`（GET query） | `page = page_num - 1`（API 0-based） |
   | §3.5 告警 | `pageNum`/`pageSize`（body） | 直通 |
   | §3.12 日志 | `pageNum`/`pageSize`（GET query） | 直通 |

   Spring 信封 `number` 为 0-based：**保持线上原名、原样回显、不加行内注释**；语义由文档（CLAUDE.md
   换算表 + 工具 description）与测试（请求 `page_num=2`→链路 `page="1"`；响应 0-based `number`
   原样解析）承载。

2. **时间窗**（§3.2/3.3/3.4/3.5/3.12）：默认 **now-1h → now**（任务要求），格式 `"%Y-%m-%d %H:%M:%S"`。
   属控制器契约 → 由 client 私有 helper 拥有，带 `now=None` 测试接缝（无需 monkeypatch）。

3. **泛型分页信封**：一个 `PageResponse[T]`（Spring 信封）替代 N 个重复模型。模型写完立即跑 mypy
   作检查点；mypy 2.3 推断不出 `Self` 时退路是局部 `cast(...)`。

4. **瘦类型规则**（最大实网击穿风险是猜错标量导致整页失败）：只给 JSON 类型确定的字符串字段 +
   嵌套对象结构加类型；数值型/未知类型字段走 `extra="allow"`，live 验证后再提升。

5. **安全（凭据剥离）**：§2.2 设备响应内联返回 Base64 `password`。`Device` 用递归 mode="before"
   校验器 `_strip_sensitive` 在校验前剥掉名为 `password` 与 `community`（SNMP 凭据）的键（含嵌套，
   如 `ftp_param.password`、`snmp_param.community`）。**只剥明确凭据**：`username` 及全部业务字段
   保留；剥离返回新结构、不就地修改入参（不可变）。精确性由测试钉死（见测试节）。

6. **校验分工**：schema 可表达的枚举用 `Literal` 工具参数（非法值 FastMCP 返回 `isError=True`，记为
   已知差异）；边界/格式（页码范围、时间串、非空 ID）返回 `{"ok": False, "detail": ...}`。

7. **告警走全新一套（不改旧契约）**：
   - 新 client 方法 `query_alert_page(...)`（§3.5 契约：startTime/endTime 必填 + 可选
     namespace/category/sourceList/autoRecovery/msg/level/pageNum/pageSize）追加到 `SDNClient` 类尾，
     **复用现有 `SDNAlertsResponse`/`SDNAlert` 模型**（其 `extra="allow"` 已能保留 traceId/serialNo/
     autoRecovery 等新字段，无需改模型）；endpoint 键沿用 `alerts`（`/monitor/v2/alert/page`）。
   - 新工具 `sdn_device_alerts(...)` 落新文件 `alert_tools.py`（预设"单设备、最近一小时"）。
   - 旧 `query_alerts`/`sdn_alerts` 及其测试**完全不动**（作为框架桩保留，上游若有更新可干净 merge）。

8. **输出**：所有新工具 `result.model_dump(by_alias=True)`，键名与控制器词汇（kebab/camel）一致。

9. **merge-friendly 布局**：对框架文件的就地改动**仅剩不可避免的 import 增补**（`client.py`、
   `models.py` 顶部），其余全部追加或新文件。

## 模块布局

**框架文件——最小改动：**
- `app/sdn/client.py`：① 顶部 import 增补（`datetime`/`timedelta`、`Sequence`、`Literal`/`get_args`、新模型名——不可避免）；② 8 个新业务方法作为**连续块追加到 `SDNClient` 类尾**（替换 `# TODO(sdn-wiring)` 占位、`aclose` 之前）；③ 新常量（`PerfPeriod`/`AlertLevel`/`ConnectStatus`/`LinkStatus`/`LinkType`/`SwitchNamespace`/`PERF_PERIODS`/`PERF_TIME_FORMAT`）+ 3 个私有 helper（`_default_time_window`/`_resolve_window`/`_perf_params`）**追加到模块文件尾**（`_map_http_error` 之后）。**不碰** `health`/`query_alerts` 等现有方法。
- `app/sdn/models.py`：新模型**追加到文件尾**；删 `# TODO(sdn-wiring)` 尾注；import 行增补 `model_validator`/`Generic`/`TypeVar`/`Any`（不可避免）。**不改**现有 `SDNHealthResponse`/`SDNAlert`/`SDNAlertsResponse`。
- `app/tools/sdn_tools.py`：**完全不动**。
- `app/tools/system.py`、`app/server.py`、`app/settings.py`、`app/sdn/exceptions.py`、`app/sdn/__init__.py`、`app/common/http.py`：不动。
- `app/tools/__init__.py`：仅**追加**新工具模块的 `register` 调用。

**新文件（merge 隔离，按域拆 7 个）：**
| 新文件 | 工具 |
|---|---|
| `app/tools/device_tools.py` | `sdn_device_by_name`、`sdn_device_by_management_ip` |
| `app/tools/link_tools.py` | `sdn_link_info` |
| `app/tools/topology_tools.py` | `sdn_topology` |
| `app/tools/perf_tools.py` | `sdn_port_traffic`、`sdn_link_performance`、`sdn_vpn_traffic`、`sdn_te_tunnel_traffic` |
| `app/tools/log_tools.py` | `sdn_operation_logs` |
| `app/tools/alert_tools.py` | `sdn_device_alerts`（新告警工具，§3.5） |
| `app/tools/validation.py` | 共享校验 helper（无 `register`，被各工具 import） |

`register_all` 追加（`validation` 是 helper，不注册）：
```python
from . import alert_tools, device_tools, link_tools, log_tools, perf_tools, sdn_tools, system, topology_tools
system.register(mcp)
sdn_tools.register(mcp)
device_tools.register(mcp)
link_tools.register(mcp)
topology_tools.register(mcp)
perf_tools.register(mcp)
log_tools.register(mcp)
alert_tools.register(mcp)
```

## 逐文件细节

### `app/sdn/client.py`（追加；常量公开供 tools import）
公开常量：`PerfPeriod = Literal["5m","1h","1d","1M"]`、`PERF_PERIODS`、`PERF_TIME_FORMAT`、
`AlertLevel = Literal["CRITICAL","MAJOR","MINOR","WARNING"]`、`ConnectStatus`、`LinkStatus`、`LinkType`、
`SwitchNamespace = Literal["port","link"]`。
私有 helper：`_default_time_window(span=timedelta(hours=1), *, now=None)`、`_resolve_window(start,end)`、
`_perf_params(namespace, metric_names, dimensions, period, start_time, end_time)`（namespace + 逗号拼接
metricNames + period + 时间窗 + 索引化 `dimensions.N.name/value`）。
新业务方法（全部照 `query_alerts` 骨架：`endpoints.get(key,default)` → `self.request(...)` →
`model_validate` → `(ValueError, ValidationError)` → `SDNError("… malformed.", detail=str(exc))`）：

```python
async def query_devices(*, page_num=1, page_size=10, name=None, management_ip=None,
    connect_status=None, vendor_id=None, platform_id=None, product_name=None,
    pe_as=None, plane_type=None, label=None, label_filter_type=None) -> PageResponse[Device]
# POST devices_page（/api/no/config/terra-pe:peInfos/page）
# params={"pageNumber": page_num, "pageSize": page_size}
# body = 非 None 过滤器，控制器键名（name, management-ip, connect-status, vendor-id,
#        platform-id, product-name, peAs(驼峰!), plane-type, label, label-filter-type）

async def query_links(*, page_num=1, page_size=10, link_id=None, source_node=None,
    destination=None, source_ip=None, destination_ip=None, status=None, link_type=None,
    link_err=None, label=None, label_filter_type=None, srv6_sid=None, srv6_sid_compare=None,
    srv6_locator=None, mpls_adj_label=None, mpls_sid_compare=None, perf_inst_type=None) -> PageResponse[LinkInfo]
# GET links_page（/api/sr/config/network-topology:network-topology/topology/linksInfo/page）
# params {page: page_num-1, size, linkId, sourceNode, destination, sourceIp, destinationIp,
#   status, type, linkErr, label, label-filter-type, srv6Sid, srv6SidCompare, srv6Locator,
#   mplsAdjLabel, mplsSidCompare, perfInstType} — 必须过滤 None

async def query_switch_history(namespace, *, metric_names, device_name=None, port_name=None,
    link_id=None, period="5m", start_time=None, end_time=None) -> PerfHistoryResponse
# GET perf_switch_history（/monitor/switch/history）
# "port" → dims [("switch", device_name), ("port", port_name)]
# "link" → dims [("linkId", link_id)]；缺必需维度 → SDNError（工具层不可达）

async def query_vpn_history(vpn_id, *, metric_names, period="5m", ...) -> PerfHistoryResponse
# GET perf_vpn_history（/monitor/vpn/history），namespace="traffic"，dims [("vpnId", vpn_id)]

async def query_te_history(device_name, tunnel_name, *, metric_names, period="5m", ...) -> PerfHistoryResponse
# GET perf_te_history（/monitor/te/history），namespace="traffic"，dims [("deviceName",...),("tunnelName",...)]

async def query_alert_page(*, start_time=None, end_time=None, namespace=None, category=None,
    source_list=None, auto_recovery: Literal[1,2,3] | None=None, msg=None, level=None,
    page_num=1, page_size=10) -> SDNAlertsResponse            # 新方法；复用现有模型
# body 固定含 startTime/endTime（默认窗）/pageNum/pageSize；可选项仅在非 None 时出现；
# sourceList 仅在非空时以 list 出现。endpoint 键 "alerts"（/monitor/v2/alert/page）

async def get_topology() -> TopologyResponse
# GET topology（/api/sr/config/network-topology:network-topology），无参数

async def query_operation_logs(*, start_time=None, end_time=None, page_num=1,
    page_size=10) -> OperationLogsResponse
# GET operation_logs（/monitor/logs），params startTime/endTime/pageNum/pageSize
```
删除 `# TODO(sdn-wiring)` 占位块。

### `app/sdn/models.py`（追加；不改现有模型）
- `PageResponse(BaseModel, Generic[T])`（`T = TypeVar("T", bound=BaseModel)`，`extra="allow"`）：
  `content: list[T]`、`total_elements`、`total_pages`、`last`、`first`、`size`、`number`、
  `number_of_elements`、`empty`（字段均无别名、无行内注释）。
- `_SENSITIVE_KEYS = frozenset({"password", "community"})` + 递归 `_strip_sensitive(obj)`（dict：删凭据键
  并对其余值递归返回新 dict；list：逐项递归返回新 list；其他原样返回；不改入参）。
- `Device`（`extra="allow", populate_by_name=True`，mode="before" 剥敏）：具名 `str | None`（kebab 别名）
  id/name（默认 ""）、pe-alias、node-type、vendor-id、platform-id、product-name、version、management-ip、
  connect-status、te-loopback-if/ip/ipv6、bgp-loopback-if/ip/ipv6、locator、pop-id、create-time、
  update-time；嵌套 `pop_info`(alias popInfo → `PopInfo`: id/name/province/city)、`label: list[DeviceLabel]`(id/name)。
  走 extras：ssh-port、management-port、username、pe-as、isis-process-id、alerts{}、snmp_param（剥
  community）、ftp_param（剥 password）、peports{}。
- `LinkInfo`：link_id("link-id") + link-status/link-type/source-ip/dest-ip/source-node-ip/dest-node-ip/
  srv6-locator；嵌套 `LinkSource`(source-node/-alias/-tp/-tp-alias)、`LinkDestination`(dest-node/-alias/-tp)。数值走 extras。
- 拓扑链：`TopologyResponse{topology: list[Topology]}` → `Topology`(topology-id、node list、link list)；
  `TopologyNode` 具名字符串 + `termination_point: list[TerminationPoint]`；`TopologyLink`(link-id、link-status + 嵌套 source/destination)。
- `PerfHistoryResponse`{data: list[PerfDataPoint], code=0, message="", success: bool|None=None}；
  `PerfDataPoint` 仅 time 具名，指标全走 extras。
- `OperationLogsResponse`{code, message, data: list[OperationLog]}；`OperationLog`：id/time + account_name(accountName)/behave_as(behaveAs)/caller/method/url/operation_desc(operationDesc)；extras：body/callerId/code/fromIp/retMsg。

### `config/sdn_controller.yaml`（替换 endpoints 块）
保留 health、`login: "/oauth/token"`、`alerts`；删 `devices` 桩；`topology` 换真实 URL；新增
`devices_page`、`links_page`、`perf_switch_history`、`perf_vpn_history`、`perf_te_history`、`operation_logs`。

### 工具层（新文件）
- `app/tools/validation.py`：`MAX_PAGE_SIZE`、`page_bounds_detail(page_num, page_size)->str|None`、
  `time_window_detail(start_time, end_time)->str|None`（要么都给要么都不给、按 `PERF_TIME_FORMAT` 解析、start≤end）。消息只回显调用方输入（安全）。
- `device_tools.py`：`sdn_device_by_name(name, page_num=1, page_size=10)`、
  `sdn_device_by_management_ip(management_ip, page_num=1, page_size=10)`（description 注明输出永不含密码）。
- `link_tools.py`：`sdn_link_info(link_id=None, source_node=None, destination=None, source_ip=None,
  destination_ip=None, status=None, link_type=None, label=None, label_filter_type=None, page_num=1, page_size=10)`。
- `topology_tools.py`：`sdn_topology()`。
- `perf_tools.py`：`sdn_port_traffic`(device_name, port_name, period="5m", start/end)→metrics in_traffic,out_traffic；
  `sdn_link_performance`(link_id, …)→jitter,rtt,loss；`sdn_vpn_traffic`(vpn_id, …)→四项；
  `sdn_te_tunnel_traffic`(device_name, tunnel_name, …)→四项。
- `log_tools.py`：`sdn_operation_logs(start_time=None, end_time=None, page_num=1, page_size=10)`。
- `alert_tools.py`：`sdn_device_alerts(device_name=None, start_time=None, end_time=None, namespace=None,
  category=None, level=None, auto_recovery=None, msg=None, page_num=1, page_size=10)` →
  `query_alert_page(source_list=[device_name] if device_name else None, …)`，预设"单设备、最近一小时"。
- 所有工具保持范式：骨架短路（精确文案 `"SDN controller not configured (skeleton mode)."`）→ 调一个
  client 方法 → `{"ok": True, "configured": True, **result.model_dump(by_alias=True)}`；
  `except SDNError`→`ctx.error`+`str(exc)`；`except Exception`→`logger.exception`+`"Unexpected server error."`。

## 测试（新增；现有 query_alerts/sdn_alerts 测试不动）
- `tests/test_sdn_client.py`：每个新方法加成功用例（断 path + 解码后 `request.url.params` + 精确 body，
  含 `peAs` 与 None 省略）、extras 经 by_alias 保留、`500→SDNHTTPError`、`b"not-json"→脱敏 SDNError`。
  专项：
  - **凭据剥离精确性**（`_strip_sensitive` 单测 + 经 `query_devices` 集成）：正向剥离顶层 `password`、
    嵌套 `ftp_param.password`、`snmp_param.community`；负向保留 `username`/`peports`/`alerts`/
    `snmp_param`(结构在、仅 community 消失)/`popInfo`/`label`；不可变性（入参不被就地改）。
  - **分页换算**：链路 `page_num=2`→`params["page"]=="1"`；响应 0-based `number` 原样解析；设备 `pageNumber` 直通。
  - 链路 `linkErr` None 不出现 / True→`"true"`；设备 endpoint 覆盖。
  - 性能：点号键原样、metricNames 逗号拼接、显式窗直通、默认窗解析后跨度 ≈1h、缺维度保护抛错；`_default_time_window` 单测（固定 now）。
- `tests/test_tools.py`：对全部 13 个工具（`ping` + 12 个 SDN 工具，含新 `sdn_device_alerts`）参数化骨架短路；各工具页码/
  时间窗/空 ID 校验；每工具成功信封（canned JSON handler），其中**设备工具输出含 management-ip/
  username/peports、绝不含 password/community**；每模块一个脱敏测试（payload 不含 URL）；每新模块一个
  `BoomClient` 意外错误测试；`period="2m"`→`isError is True`；`test_list_tools_includes_system_tools`
  期望名单加新工具。**不重写**旧 alerts 块。
- `tests/test_smoke.py`：扩展工具存在性断言。`conftest.py` 无需改动。
- `scripts/test_sdn_live.py`：扩成 ~8 步（登录、query_devices+无密码断言、query_links、get_topology
  计数、用首台设备/端口跑端口性能、用首个 link-id 跑链路性能、**新** query_alert_page、
  query_operation_logs），逐步容错 + PASS/FAIL 汇总。职责：用真实控制器验证瘦类型猜测。

## 文档（实施时同步；`CLAUDE.md` 与 `AGENTS.md` 相同改动）+ README
§2 测试数；§3 目录树（新模块）；§5 工具规范（by_alias 规则、校验分工、1-based 分页 + Spring `number`
0-based 原样回显）；§5 client 规范（页码换算表、时间窗归属、endpoint 键→方法表）；§5 models 规范
（`PageResponse[T]`、瘦类型、递归凭据剥离只剥 password/community 保留 username）；§8.1 注明**告警新走
`query_alert_page`/`sdn_device_alerts`，旧 `query_alerts`/`sdn_alerts` 保留作框架桩**；§9 live 脚本作
schema 验证环节。

## 实施顺序与质量门（TDD：每层先写测试 RED，再实现 GREEN）
1. models.py（追加）→ **立即 `uv run mypy`**（泛型检查点；退路 cast→具体信封类）。
2. client.py（常量+helper 追加模块尾、8 新方法追加类尾）→ mypy。
3. YAML endpoints。
4. validation.py → device/link/topology/perf/log/alert tools → `__init__.py` → mypy + `ruff check .`。
5. 测试套件（新增，不动旧 alerts 测试）→ `uv run pytest`（覆盖率门 ≥80%）。
6. live 脚本扩展；CLAUDE.md + AGENTS.md + README 同步。
7. 终检：`uv run ruff check . && uv run mypy && uv run pytest`。

## 端到端验证
- `uv run pytest` 全绿、覆盖率 ≥80%；`ruff`+`mypy` 干净。
- `uv run sdn-mcp` 起服 → `scripts/test_client.py list-tools` 列出全部工具（含新 10 个）；骨架模式逐个
  `call-tool` 返回 `{"ok": false, "configured": false, ...}`。
- 有真实控制器：`PYTHONPATH=. uv run python scripts/test_sdn_live.py` 验证线上契约，schema 差异回填 models。

## 遗留项（标注不阻塞，待 live 验证）
- 告警 `pageNum` 基数（文档正文 0-based vs 示例 1）→ 按 1 实施，live 验证。
- `autoRecovery` 线上类型（int vs string）— live 验证。
- 链路/拓扑数值型指标暂走 extras，live 后提升。
- 旧 `sdn_alerts` 工具契约错误（interval-based）—— 本计划刻意保留作框架桩；是否禁用/隐藏由后续产品决定。
- 建议把 AGENTS.md 做成 CLAUDE.md 的 symlink（另开清理任务）。

## 工具清单（现有 3 + 新增 10 = 13；旧 sdn_alerts 保留）
`ping`、`sdn_health`、`sdn_alerts`（旧桩，保留不动）｜
新增：`sdn_device_by_name`、`sdn_device_by_management_ip`、`sdn_link_info`、`sdn_topology`、
`sdn_port_traffic`、`sdn_link_performance`、`sdn_vpn_traffic`、`sdn_te_tunnel_traffic`、
`sdn_operation_logs`、`sdn_device_alerts`
