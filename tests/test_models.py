"""Pure unit tests for SDN response models (boundary validation, no IO).

Covers the new v1.5 models added in ``app/sdn/models.py``: the generic Spring
page envelope, the recursive credential scrubber, and the kebab/camel alias
round-tripping for device / link / topology / perf / operation-log entities.
"""

from __future__ import annotations

import copy

from app.sdn.models import (
    CommandResultResponse,
    Device,
    LinkInfo,
    OperationLog,
    OperationLogsResponse,
    PageResponse,
    PerfDataPoint,
    PerfHistoryResponse,
    Topology,
    TopologyNode,
    TopologyResponse,
    _strip_sensitive,
)


def _device_payload() -> dict[str, object]:
    """A representative §2.2 device object, including in-band credentials."""
    return {
        "id": "174f455f-6f96-40d3-a7a9-65da3ddb2cc8",
        "name": "NJ-CORE-RT-ZX-M6KS-02-WLC11",
        "pe-alias": "NJ-CORE-RT-ZX-M6KS-02-WLC11",
        "node-type": "PE",
        "vendor-id": "ZTE",
        "platform-id": "ZXCTN6180",
        "product-name": "ZXR10 M6000-8S Plus",
        "version": "srv6,qdx,V5.00.10.70",
        "management-ip": "192.168.118.200",
        "connect-status": "UP",
        "te-loopback-if": "loopback1",
        "te-loopback-ip": "172.16.20.66",
        "pop-id": "3c57e489-d72e-46d7-8b08-0decd40a1348",
        "create-time": "2026-03-31 18:11:34",
        "update-time": "2026-04-24 09:32:29",
        "username": "ceni",
        "ssh-port": 22,
        "management-port": "830",
        "pe-as": 64999,
        "password": "Q2VuaUAhQCM0NTY=",
        "popInfo": {
            "id": "3c57e489-d72e-46d7-8b08-0decd40a1348",
            "name": "POP_ZTE",
            "province": "江苏",
            "city": "南京",
            "pop-as": 64999,
        },
        "label": [
            {"id": "a95f0f6c", "name": "PE", "label-type": "AUTO", "is-health-label": False}
        ],
        "alerts": {"warning": 0, "minor": 0, "major": 0, "critical": 0},
        "ftp_param": {"server_ip": "", "protocol": "SFTP", "vrf": "", "password": "ftp-secret"},
        "snmp_param": {
            "community": "private",
            "snmp_version": "v3",
            "port": 161,
            "security_level": "AuthPriv",
        },
        "peports": {
            "peport-info": [
                {"id": "p1", "name": "xgei-0/0/0/10", "status": "UP", "total-bandwidth": 10000}
            ]
        },
    }


# --- _strip_sensitive: credential scrubber ---------------------------------


def test_strip_sensitive_removes_top_level_password() -> None:
    out = _strip_sensitive({"name": "d1", "password": "x"})
    assert out == {"name": "d1"}


def test_strip_sensitive_removes_nested_credentials() -> None:
    out = _strip_sensitive(
        {
            "password": "top",
            "ftp_param": {"protocol": "SFTP", "password": "ftp-secret"},
            "snmp_param": {"community": "private", "port": 161},
        }
    )
    assert out == {
        "ftp_param": {"protocol": "SFTP"},
        "snmp_param": {"port": 161},
    }


def test_strip_sensitive_preserves_business_fields() -> None:
    out = _strip_sensitive(
        {
            "username": "ceni",
            "peports": {"peport-info": []},
            "alerts": {"major": 1},
            "popInfo": {"name": "POP"},
            "label": [{"name": "PE"}],
            "snmp_param": {"community": "private", "port": 161},
        }
    )
    assert out["username"] == "ceni"
    assert out["peports"] == {"peport-info": []}
    assert out["alerts"] == {"major": 1}
    assert out["popInfo"] == {"name": "POP"}
    assert out["label"] == [{"name": "PE"}]
    assert out["snmp_param"] == {"port": 161}


def test_strip_sensitive_does_not_mutate_input() -> None:
    original = {"name": "d1", "password": "x", "ftp_param": {"password": "y"}}
    snapshot = copy.deepcopy(original)
    _strip_sensitive(original)
    assert original == snapshot


def test_strip_sensitive_handles_lists() -> None:
    out = _strip_sensitive([{"password": "a"}, {"name": "keep"}, 7])
    assert out == [{}, {"name": "keep"}, 7]


def test_strip_sensitive_passthrough_scalars() -> None:
    assert _strip_sensitive("str") == "str"
    assert _strip_sensitive(7) == 7
    assert _strip_sensitive(None) is None


# --- Device / PageResponse --------------------------------------------------


def test_device_page_parses_envelope_and_preserves_pageable() -> None:
    page = PageResponse[Device].model_validate(
        {
            "content": [_device_payload()],
            "pageable": {"page_number": 0, "page_size": 10, "paged": True},
            "sort": {"sorted": False, "unsorted": True, "empty": True},
            "total_elements": 6,
            "total_pages": 1,
            "last": True,
            "first": True,
            "size": 10,
            "number": 0,
            "number_of_elements": 1,
            "empty": False,
        }
    )
    assert page.total_elements == 6
    assert page.number == 0
    device = page.content[0]
    assert isinstance(device, Device)
    assert device.management_ip == "192.168.118.200"
    assert device.connect_status == "UP"
    assert device.pop_info is not None and device.pop_info.name == "POP_ZTE"
    dumped = page.model_dump(by_alias=True)
    assert dumped["pageable"]["page_size"] == 10
    assert dumped["sort"]["unsorted"] is True


def test_device_strips_credentials_top_level_and_nested() -> None:
    page = PageResponse[Device].model_validate({"content": [_device_payload()]})
    device = page.content[0].model_dump(by_alias=True)
    assert "password" not in device
    assert "password" not in device["ftp_param"]
    assert "community" not in device["snmp_param"]


def test_device_preserves_username_and_business_fields() -> None:
    page = PageResponse[Device].model_validate({"content": [_device_payload()]})
    device = page.content[0].model_dump(by_alias=True)
    assert device["username"] == "ceni"
    assert device["peports"]["peport-info"][0]["name"] == "xgei-0/0/0/10"
    assert device["alerts"]["critical"] == 0
    assert device["popInfo"]["name"] == "POP_ZTE"
    assert device["label"][0]["name"] == "PE"


def test_device_dump_by_alias_uses_controller_vocabulary() -> None:
    page = PageResponse[Device].model_validate({"content": [_device_payload()]})
    device = page.content[0].model_dump(by_alias=True)
    assert "management-ip" in device
    assert "connect-status" in device
    assert "te-loopback-if" in device
    assert "management_ip" not in device


# --- LinkInfo ---------------------------------------------------------------


def test_link_info_parses_and_dumps_by_alias() -> None:
    page = PageResponse[LinkInfo].model_validate(
        {
            "content": [
                {
                    "link-id": "NJ-SCT-R04:GE0/13/4>NJ-SCT-R03:GE5/1/12",
                    "link-status": "DOWN",
                    "link-type": "BACKBONE",
                    "source-ip": "172.21.22.6/30",
                    "dest-ip": "172.21.22.5/30",
                    "source-node-ip": "192.168.118.14",
                    "dest-node-ip": "192.168.118.13",
                    "srv6-locator": "SRv6",
                    "loss": 100.0,
                    "delay": 0.0,
                    "jitter": 0.0,
                    "source": {
                        "source-node": "NJ-SCT-R04",
                        "source-node-alias": "SCT-NJ-SCT-R04",
                        "source-tp": "GigabitEthernet0/13/4",
                    },
                    "destination": {
                        "dest-node": "NJ-SCT-R03",
                        "dest-tp": "Ten-GigabitEthernet5/1/12",
                    },
                }
            ],
            "total_elements": 22,
            "number": 2,
        }
    )
    link = page.content[0]
    assert link.link_id == "NJ-SCT-R04:GE0/13/4>NJ-SCT-R03:GE5/1/12"
    assert link.source is not None and link.source.source_node == "NJ-SCT-R04"
    assert link.destination is not None and link.destination.dest_node == "NJ-SCT-R03"
    dumped = link.model_dump(by_alias=True)
    assert "link-id" in dumped
    assert dumped["source"]["source-node"] == "NJ-SCT-R04"
    assert dumped["loss"] == 100.0


# --- Topology ---------------------------------------------------------------


def test_topology_response_parses_nested_structure() -> None:
    resp = TopologyResponse.model_validate(
        {
            "topology": [
                {
                    "topology-id": "dciwan1",
                    "node": [
                        {
                            "node-id": "NJ-SCT-SW01",
                            "name": "NJ-SCT-SW01",
                            "node-type": "pe",
                            "node-status": "UP",
                            "vendor-id": "H3C",
                            "management-ip": "192.168.118.15",
                            "termination-point": [
                                {
                                    "tp-id": "HundredGigE1/0/25",
                                    "tp-status": "UP",
                                    "type": "PHYSICAL",
                                }
                            ],
                        }
                    ],
                    "link": [
                        {
                            "link-id": "NJ-SCT-R03:RA4>NJ-SCT-R02:Eth-Trunk4",
                            "link-status": "UP",
                            "oper-bw": 356878,
                            "delay": 1,
                            "source": {
                                "source-node": "NJ-SCT-R03",
                                "source-tp": "Route-Aggregation4",
                            },
                            "destination": {"dest-node": "NJ-SCT-R02", "dest-tp": "Eth-Trunk4"},
                        }
                    ],
                }
            ]
        }
    )
    topo = resp.topology[0]
    assert isinstance(topo, Topology)
    assert topo.topology_id == "dciwan1"
    node = topo.node[0]
    assert isinstance(node, TopologyNode)
    assert node.node_id == "NJ-SCT-SW01"
    assert node.termination_point[0].tp_id == "HundredGigE1/0/25"
    link = topo.link[0]
    assert link.link_id == "NJ-SCT-R03:RA4>NJ-SCT-R02:Eth-Trunk4"
    assert link.source is not None and link.source.source_tp == "Route-Aggregation4"
    assert link.destination is not None and link.destination.dest_node == "NJ-SCT-R02"


# --- Perf -------------------------------------------------------------------


def test_perf_response_parses_code_message_envelope() -> None:
    resp = PerfHistoryResponse.model_validate(
        {
            "code": 0,
            "message": "请求成功",
            "data": [
                {
                    "time": "2021-12-23 00:00:00",
                    "in_traffic": 1.67,
                    "out_traffic": 1.32,
                    "name": "GE2/0/1",
                }
            ],
        }
    )
    assert resp.code == 0
    assert resp.data[0].time == "2021-12-23 00:00:00"
    assert resp.data[0].model_dump()["in_traffic"] == 1.67


def test_perf_response_parses_success_envelope() -> None:
    resp = PerfHistoryResponse.model_validate(
        {
            "success": True,
            "code": 0,
            "message": "请求成功",
            "data": [
                {
                    "time": "2026-04-24 16:40:00",
                    "jitter": 1,
                    "hn1": "NJ-SCT-R02",
                    "hn2": "NJ-SCT-R01",
                }
            ],
        }
    )
    assert resp.success is True
    assert resp.data[0].model_dump()["jitter"] == 1


def test_perf_data_point_defaults() -> None:
    point = PerfDataPoint.model_validate({"time": "t"})
    assert point.time == "t"


# --- Operation logs ---------------------------------------------------------


def test_operation_logs_parses_camel_case_and_extras() -> None:
    resp = OperationLogsResponse.model_validate(
        {
            "code": 0,
            "message": "请求成功",
            "data": [
                {
                    "id": "9062bad0",
                    "time": "2023-11-13 17:51:12",
                    "accountName": "ops",
                    "behaveAs": "ADMIN",
                    "caller": "zhangsan",
                    "callerId": "RealSpan(...)",
                    "method": "POST",
                    "fromIp": "100.127.228.112",
                    "operationDesc": "",
                    "retMsg": '{"vpnId":"l3_3171"}',
                    "url": "/api/no/config/ietf-l3vpn-ntw:l3vpn-ntw",
                }
            ],
        }
    )
    log = resp.data[0]
    assert isinstance(log, OperationLog)
    assert log.account_name == "ops"
    assert log.behave_as == "ADMIN"
    assert log.operation_desc == ""
    dumped = log.model_dump(by_alias=True)
    assert dumped["accountName"] == "ops"
    assert dumped["behaveAs"] == "ADMIN"
    assert dumped["fromIp"] == "100.127.228.112"
    assert dumped["retMsg"] == '{"vpnId":"l3_3171"}'


# --- Command result ---------------------------------------------------------


def test_command_result_response_parses_result() -> None:
    resp = CommandResultResponse.model_validate({"result": "dis ip in br\r\r\nGE4/1/1 up"})
    assert resp.result.startswith("dis ip in br")
    # output text is preserved verbatim (no line-ending normalization)
    assert "\r\r\n" in resp.result


def test_command_result_response_defaults_and_extras() -> None:
    resp = CommandResultResponse.model_validate({"result": "ok", "code": 0})
    assert resp.result == "ok"
    assert resp.model_dump()["code"] == 0  # extra field preserved
