"""端到端 + 单元测试：覆盖任务要求的全部行为。

运行：/tmp/bpp-venv/bin/python -m pytest tests/ -v
"""

from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from app.conversion import COMPAT_ANNOTATION, GROUP
from app.examples import (
    CORRUPTED_ANNOTATION_REQUEST,
    CREATE_REQUEST,
    MISSING_ANNOTATION_REQUEST,
    READ_ANNOTATION,
    READ_REQUEST,
    READD_REQUEST,
    STORED_V1BETA1,
    WRITEBACK_REQUEST,
    review,
)
from app.main import app

V1ALPHA1 = f"{GROUP}/v1alpha1"
V1BETA1 = f"{GROUP}/v1beta1"

client = TestClient(app)


def convert(payload: dict) -> dict:
    resp = client.post("/convert", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "ConversionReview"
    return body["response"]


def ok(response: dict) -> list:
    assert response["result"]["status"] == "Success", response
    return response["convertedObjects"]


def fail(response: dict) -> str:
    assert response["result"]["status"] == "Failure", response
    assert "convertedObjects" not in response, "失败时不得返回 convertedObjects"
    return response["result"]["message"]


# ---------------------------------------------------------------------------
# ① 读取：v1beta1 -> v1alpha1
# ---------------------------------------------------------------------------

def test_read_flattens_and_stashes_explicit_retentions():
    [obj] = ok(convert(copy.deepcopy(READ_REQUEST)))
    assert obj["apiVersion"] == V1ALPHA1
    assert obj["spec"] == {"defaultRetention": 3, "targets": ["db", "cache", "logs"]}
    # cache 的 3 恰好等于默认值 3，也必须作为显式设置进入注解
    assert json.loads(obj["metadata"]["annotations"][COMPAT_ANNOTATION]) == {
        "v": 1,
        "retentions": {"db": 5, "cache": 3},
    }
    # 注解内容确定性输出（幂等）
    assert obj["metadata"]["annotations"][COMPAT_ANNOTATION] == READ_ANNOTATION
    # 其它 metadata 原样保留
    assert obj["metadata"]["labels"] == {"team": "payments"}
    assert obj["metadata"]["annotations"]["team.example.com/owner"] == "payments-oncall"
    assert obj["metadata"]["resourceVersion"] == "18231"


# ---------------------------------------------------------------------------
# ② 写回：改默认值 + 重排，按名字恢复；"等于旧默认"也保持显式
# ---------------------------------------------------------------------------

def test_writeback_restores_by_name_even_when_equal_to_old_default():
    [obj] = ok(convert(copy.deepcopy(WRITEBACK_REQUEST)))
    assert obj["apiVersion"] == V1BETA1
    assert obj["spec"]["defaultRetention"] == 10  # 旧客户端改的默认值生效
    # 重排后的顺序保留；按名字恢复单独设置
    assert obj["spec"]["targets"] == [
        {"name": "logs"},                    # 本来就跟随默认
        {"name": "cache", "retention": 3},   # 等于旧默认 3，仍是显式设置，不跟随新默认 10
        {"name": "db", "retention": 5},
    ]
    # 兼容注解不进入存储版本
    assert COMPAT_ANNOTATION not in obj["metadata"].get("annotations", {})
    # 业务注解保留
    assert obj["metadata"]["annotations"]["team.example.com/owner"] == "payments-oncall"


def test_writeback_add_and_remove_targets():
    """旧客户端删掉 cache、新增 metrics：cache 的设置随之消失，metrics 跟随默认。"""
    payload = copy.deepcopy(WRITEBACK_REQUEST)
    payload["request"]["objects"][0]["spec"]["targets"] = ["db", "metrics"]
    [obj] = ok(convert(payload))
    assert obj["spec"]["targets"] == [
        {"name": "db", "retention": 5},
        {"name": "metrics"},  # 新增目标：注解里没有 -> 跟随默认
    ]


# ---------------------------------------------------------------------------
# ③ 删除后同名重加 -> 沿用当前默认（完整链路）
# ---------------------------------------------------------------------------

def test_remove_then_readd_uses_current_default():
    # 读
    [v1alpha1_obj] = ok(convert(copy.deepcopy(READ_REQUEST)))
    # 写回 1：删掉 cache
    v1alpha1_obj["spec"]["targets"] = ["db", "logs"]
    [stored] = ok(convert(review("t1", V1BETA1, [v1alpha1_obj])))
    assert [t["name"] for t in stored["spec"]["targets"]] == ["db", "logs"]
    # 再读：注解里不应再有 cache
    [v1alpha1_obj2] = ok(convert(review("t2", V1ALPHA1, [stored])))
    assert json.loads(v1alpha1_obj2["metadata"]["annotations"][COMPAT_ANNOTATION])["retentions"] == {"db": 5}
    # 写回 2：同名加回 cache
    v1alpha1_obj2["spec"]["targets"] = ["db", "logs", "cache"]
    [stored2] = ok(convert(review("t3", V1BETA1, [v1alpha1_obj2])))
    assert stored2["spec"]["targets"] == [
        {"name": "db", "retention": 5},
        {"name": "logs"},
        {"name": "cache"},  # 无 retention -> 沿用当前默认
    ]


def test_readd_example_payload():
    [obj] = ok(convert(copy.deepcopy(READD_REQUEST)))
    assert obj["spec"]["targets"] == [
        {"name": "db", "retention": 5},
        {"name": "cache"},  # 删后重加：沿用当前默认
    ]


# ---------------------------------------------------------------------------
# ④ 重启安全：webhook 无状态
# ---------------------------------------------------------------------------

def test_restart_between_conversions():
    """读和写回分别由两个全新的应用实例完成（模拟进程重启）。"""
    [v1alpha1_obj] = ok(TestClient(app).post("/convert", json=copy.deepcopy(READ_REQUEST)).json()["response"])
    v1alpha1_obj["spec"]["defaultRetention"] = 10
    fresh = TestClient(app)  # 新实例 == 重启后
    resp = fresh.post("/convert", json=review("t-restart", V1BETA1, [v1alpha1_obj]))
    [obj] = ok(resp.json()["response"])
    assert obj["spec"]["targets"][1] == {"name": "cache", "retention": 3}


# ---------------------------------------------------------------------------
# ⑤ 丢注解 / 注解损坏：整批失败并指出问题对象
# ---------------------------------------------------------------------------

def test_missing_annotation_fails_whole_batch_and_names_object():
    message = fail(convert(copy.deepcopy(MISSING_ANNOTATION_REQUEST)))
    assert "1 of 2" in message                       # 整批失败
    assert 'BackupPolicy "default/audit-backup"' in message  # 指出问题对象
    assert COMPAT_ANNOTATION in message              # 指出缺哪个注解
    assert "[1]" in message                          # 带对象序号


def test_corrupted_annotation_fails_and_names_object():
    message = fail(convert(copy.deepcopy(CORRUPTED_ANNOTATION_REQUEST)))
    assert 'BackupPolicy "default/web-backup"' in message
    assert "corrupted" in message
    assert COMPAT_ANNOTATION in message


@pytest.mark.parametrize(
    "bad",
    [
        "{not-json",                                  # 非法 JSON
        "[1,2]",                                      # 不是对象
        '{"v":2,"retentions":{}}',                    # 不支持的 payload 版本
        '{"v":1,"retentions":{"db":"5"}}',            # 份数不是整数
        '{"v":1,"retentions":{"db":-1}}',             # 负数
        '{"v":1,"retentions":{"db":true}}',           # bool 不是整数
        '{"v":1}',                                    # 缺 retentions
    ],
)
def test_corrupted_annotation_variants(bad):
    payload = copy.deepcopy(CORRUPTED_ANNOTATION_REQUEST)
    payload["request"]["objects"][0]["metadata"]["annotations"][COMPAT_ANNOTATION] = bad
    message = fail(convert(payload))
    assert "corrupted" in message and "web-backup" in message


# ---------------------------------------------------------------------------
# ⑥ 旧版新建
# ---------------------------------------------------------------------------

def test_create_without_annotation_succeeds():
    [obj] = ok(convert(copy.deepcopy(CREATE_REQUEST)))
    assert obj["spec"] == {
        "defaultRetention": 4,
        "targets": [{"name": "db"}, {"name": "logs"}],
    }


def test_create_with_valid_annotation_is_honored():
    """克隆场景：新建时带了合法注解（如从旧对象拷来），按注解恢复。"""
    payload = copy.deepcopy(CREATE_REQUEST)
    payload["request"]["objects"][0]["metadata"]["annotations"] = {
        COMPAT_ANNOTATION: '{"v":1,"retentions":{"db":7}}'
    }
    [obj] = ok(convert(payload))
    assert obj["spec"]["targets"][0] == {"name": "db", "retention": 7}


def test_create_with_corrupted_annotation_fails():
    payload = copy.deepcopy(CREATE_REQUEST)
    payload["request"]["objects"][0]["metadata"]["annotations"] = {
        COMPAT_ANNOTATION: "garbage"
    }
    assert "corrupted" in fail(convert(payload))


# ---------------------------------------------------------------------------
# 健壮性 / 协议
# ---------------------------------------------------------------------------

def test_duplicate_targets_rejected():
    payload = copy.deepcopy(CREATE_REQUEST)
    payload["request"]["objects"][0]["spec"]["targets"] = ["db", "db"]
    assert "duplicate" in fail(convert(payload))


def test_round_trip_is_idempotent():
    """v1beta1 -> v1alpha1 -> v1beta1 必须还原原 spec（不动点）。"""
    [v1alpha1_obj] = ok(convert(copy.deepcopy(READ_REQUEST)))
    [back] = ok(convert(review("t-rt", V1BETA1, [v1alpha1_obj])))
    assert back["spec"] == STORED_V1BETA1["spec"]
    assert back["metadata"]["labels"] == STORED_V1BETA1["metadata"]["labels"]


def test_same_version_passthrough():
    payload = review("t-pass", V1BETA1, [copy.deepcopy(STORED_V1BETA1)])
    [obj] = ok(convert(payload))
    assert obj == STORED_V1BETA1


def test_unknown_desired_version_fails():
    payload = review("t-bad", f"{GROUP}/v9", [copy.deepcopy(STORED_V1BETA1)])
    assert "unsupported desiredAPIVersion" in fail(convert(payload))


def test_malformed_review_fails():
    resp = client.post("/convert", json={"apiVersion": "apiextensions.k8s.io/v1"})
    assert resp.status_code == 200
    assert resp.json()["response"]["result"]["status"] == "Failure"


def test_uid_is_echoed():
    response = convert(copy.deepcopy(READ_REQUEST))
    assert response["uid"] == "demo-0001"


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}
