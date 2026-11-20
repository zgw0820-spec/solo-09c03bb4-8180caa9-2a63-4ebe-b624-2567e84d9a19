#!/usr/bin/env python3
"""对运行中的 webhook 实测"旧客户端读 -> 改 -> 写回"全流程。

用法：
    python3 scripts/demo.py [BASE_URL]          # 默认 http://127.0.0.1:8080

步骤 1-2 之间可以重启 webhook（如 docker compose restart webhook），
随后继续跑步骤 2，验证重启不影响写回。
"""

from __future__ import annotations

import copy
import json
import sys

import httpx

sys.path.insert(0, ".")
from app.conversion import COMPAT_ANNOTATION, GROUP  # noqa: E402
from app.examples import READ_REQUEST  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
V1ALPHA1 = f"{GROUP}/v1alpha1"
V1BETA1 = f"{GROUP}/v1beta1"


def convert(uid: str, desired: str, objects: list[dict]) -> dict:
    payload = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "ConversionReview",
        "request": {"uid": uid, "desiredAPIVersion": desired, "objects": objects},
    }
    resp = httpx.post(f"{BASE}/convert", json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()["response"]


def show(title: str, obj) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    print(f"webhook: {BASE}")
    assert httpx.get(f"{BASE}/healthz", timeout=5).json()["status"] == "ok"

    # ---- 步骤 1：旧客户端读取（v1beta1 -> v1alpha1）----
    resp = convert("demo-1", V1ALPHA1, copy.deepcopy(READ_REQUEST["request"]["objects"]))
    old_view = resp["convertedObjects"][0]
    show("步骤1 旧客户端读到的 v1alpha1（注意兼容注解）", old_view)
    ann = json.loads(old_view["metadata"]["annotations"][COMPAT_ANNOTATION])
    assert ann["retentions"] == {"cache": 3, "db": 5}, "cache=3 虽等于默认值也必须记录"

    # ---- 步骤 2：旧客户端改默认值 + 重排目标，写回（v1alpha1 -> v1beta1）----
    old_view["spec"]["defaultRetention"] = 10
    old_view["spec"]["targets"] = ["logs", "cache", "db"]  # 重排
    resp = convert("demo-2", V1BETA1, [old_view])
    stored = resp["convertedObjects"][0]
    show("步骤2 写回后的 v1beta1（默认值 3->10，目标重排）", stored["spec"])
    assert stored["spec"]["targets"] == [
        {"name": "logs"},
        {"name": "cache", "retention": 3},  # 等于旧默认也保持显式
        {"name": "db", "retention": 5},
    ]
    assert COMPAT_ANNOTATION not in stored["metadata"].get("annotations", {})

    # ---- 步骤 3：删掉 cache 写回 -> 再读 -> 同名加回 -> 沿用当前默认 ----
    old_view2 = copy.deepcopy(old_view)
    old_view2["spec"]["targets"] = ["db", "logs"]  # 删除 cache
    stored = convert("demo-3a", V1BETA1, [old_view2])["convertedObjects"][0]
    reread = convert("demo-3b", V1ALPHA1, [stored])["convertedObjects"][0]
    assert json.loads(reread["metadata"]["annotations"][COMPAT_ANNOTATION])["retentions"] == {"db": 5}
    reread["spec"]["targets"] = ["db", "logs", "cache"]  # 同名加回
    stored = convert("demo-3c", V1BETA1, [reread])["convertedObjects"][0]
    show("步骤3 删除 cache 后又同名加回：cache 不再有单独份数", stored["spec"]["targets"])
    assert stored["spec"]["targets"][-1] == {"name": "cache"}, "删后重加应沿用当前默认"

    # ---- 步骤 4：带 resourceVersion 写回但丢注解 -> 整批失败并指名 ----
    broken = copy.deepcopy(old_view)
    broken["metadata"].pop("annotations", None)
    other = {
        "apiVersion": V1ALPHA1,
        "kind": "BackupPolicy",
        "metadata": {
            "name": "ok-one",
            "namespace": "default",
            "resourceVersion": "20001",
            "annotations": {COMPAT_ANNOTATION: '{"retentions":{},"v":1}'},  # 合法对象
        },
        "spec": {"defaultRetention": 1, "targets": ["x"]},
    }
    resp = convert("demo-4", V1BETA1, [other, broken])
    show("步骤4 丢注解写回：整批 Failure（合法对象也不转换）", resp["result"])
    assert resp["result"]["status"] == "Failure"
    assert "convertedObjects" not in resp
    assert "1 of 2" in resp["result"]["message"]  # 只有 web-backup 一个坏对象
    assert "web-backup" in resp["result"]["message"] and COMPAT_ANNOTATION in resp["result"]["message"]

    print("\n全部断言通过 ✅")


if __name__ == "__main__":
    main()
