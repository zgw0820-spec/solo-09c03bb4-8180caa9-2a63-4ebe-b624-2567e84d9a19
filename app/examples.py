"""Swagger 演示 / 自测共用的 ConversionReview 示例载荷。

示例串起来就是"旧客户端读 -> 改 -> 写回"的完整故事，编号即建议的演示顺序。
"""

from __future__ import annotations

from .conversion import COMPAT_ANNOTATION, GROUP

V1ALPHA1 = f"{GROUP}/v1alpha1"
V1BETA1 = f"{GROUP}/v1beta1"


def review(uid: str, desired: str, objects: list[dict]) -> dict:
    return {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "ConversionReview",
        "request": {"uid": uid, "desiredAPIVersion": desired, "objects": objects},
    }


# 集群里存储的 v1beta1 对象：
#   db    显式 5 份
#   cache 显式 3 份 —— 恰好等于默认值 3，用来演示"等于默认也要保留显式设置"
#   logs  未设置，跟随默认
STORED_V1BETA1 = {
    "apiVersion": V1BETA1,
    "kind": "BackupPolicy",
    "metadata": {
        "name": "web-backup",
        "namespace": "default",
        "uid": "8f7c1c2e-0000-4000-8000-abcdef000001",
        "resourceVersion": "18231",
        "labels": {"team": "payments"},
        "annotations": {"team.example.com/owner": "payments-oncall"},
    },
    "spec": {
        "defaultRetention": 3,
        "targets": [
            {"name": "db", "retention": 5},
            {"name": "cache", "retention": 3},
            {"name": "logs"},
        ],
    },
}

# ① 旧客户端读取：API server 把存储的 v1beta1 交给 webhook，要 v1alpha1。
READ_REQUEST = review("demo-0001", V1ALPHA1, [STORED_V1BETA1])

# ① 的转换结果中应出现的兼容注解内容（与转换器输出保持一致：key 排序、紧凑分隔符）。
READ_ANNOTATION = '{"retentions":{"cache":3,"db":5},"v":1}'

# ② 旧客户端写回：默认值 3 -> 10，目标重排为 [logs, cache, db]，带上①拿到的注解。
WRITEBACK_REQUEST = review(
    "demo-0002",
    V1BETA1,
    [
        {
            "apiVersion": V1ALPHA1,
            "kind": "BackupPolicy",
            "metadata": {
                "name": "web-backup",
                "namespace": "default",
                "uid": "8f7c1c2e-0000-4000-8000-abcdef000001",
                "resourceVersion": "18231",
                "labels": {"team": "payments"},
                "annotations": {
                    "team.example.com/owner": "payments-oncall",
                    COMPAT_ANNOTATION: READ_ANNOTATION,
                },
            },
            "spec": {"defaultRetention": 10, "targets": ["logs", "cache", "db"]},
        }
    ],
)

# ③ 删除后再同名添加：上一轮写回把 cache 删了，重新读取后注解里只剩 db；
#    这次旧客户端又把 cache 加回列表 —— 它应当"沿用当前默认"，不带单独份数。
READD_REQUEST = review(
    "demo-0003",
    V1BETA1,
    [
        {
            "apiVersion": V1ALPHA1,
            "kind": "BackupPolicy",
            "metadata": {
                "name": "web-backup",
                "namespace": "default",
                "uid": "8f7c1c2e-0000-4000-8000-abcdef000001",
                "resourceVersion": "19002",
                "annotations": {COMPAT_ANNOTATION: '{"v":1,"retentions":{"db":5}}'},
            },
            "spec": {"defaultRetention": 10, "targets": ["db", "cache"]},
        }
    ],
)

# ④ 带 resourceVersion 写回但丢了兼容注解：整批失败，并指出问题对象。
#    第一个对象完全合法，用来证明"整批"语义——它也不会被转换返回。
MISSING_ANNOTATION_REQUEST = review(
    "demo-0004",
    V1BETA1,
    [
        {
            "apiVersion": V1ALPHA1,
            "kind": "BackupPolicy",
            "metadata": {
                "name": "web-backup",
                "namespace": "default",
                "resourceVersion": "19002",
                "annotations": {COMPAT_ANNOTATION: '{"v":1,"retentions":{"db":5}}'},
            },
            "spec": {"defaultRetention": 10, "targets": ["db"]},
        },
        {
            "apiVersion": V1ALPHA1,
            "kind": "BackupPolicy",
            "metadata": {
                "name": "audit-backup",
                "namespace": "default",
                "resourceVersion": "19007",
            },
            "spec": {"defaultRetention": 2, "targets": ["audit-log"]},
        },
    ],
)

# ⑤ 兼容注解被改坏：整批失败并指出问题对象。
CORRUPTED_ANNOTATION_REQUEST = review(
    "demo-0005",
    V1BETA1,
    [
        {
            "apiVersion": V1ALPHA1,
            "kind": "BackupPolicy",
            "metadata": {
                "name": "web-backup",
                "namespace": "default",
                "resourceVersion": "19002",
                "annotations": {COMPAT_ANNOTATION: "{not-json"},
            },
            "spec": {"defaultRetention": 10, "targets": ["db"]},
        }
    ],
)

# ⑥ 旧客户端新建：没有 resourceVersion，也不需要注解。
CREATE_REQUEST = review(
    "demo-0006",
    V1BETA1,
    [
        {
            "apiVersion": V1ALPHA1,
            "kind": "BackupPolicy",
            "metadata": {"name": "new-backup", "namespace": "default"},
            "spec": {"defaultRetention": 4, "targets": ["db", "logs"]},
        }
    ],
)

OPENAPI_EXAMPLES = {
    "01_read": {
        "summary": "① 旧客户端读取（v1beta1 → v1alpha1）",
        "description": "API server 把存储的 v1beta1 对象交给 webhook 要 v1alpha1。"
        "响应里 per-target 设置被收进兼容注解；cache 的 3 恰好等于默认值，也照样记录。",
        "value": READ_REQUEST,
    },
    "02_writeback_change_default_and_reorder": {
        "summary": "② 旧客户端写回：改默认值 + 重排目标（v1alpha1 → v1beta1）",
        "description": "默认值 3→10、目标重排为 [logs, cache, db]，带上①的注解。"
        "响应中 db=5、cache=3 仍按名字恢复为显式设置（cache 不会跟随新默认 10），"
        "logs 无显式设置，兼容注解被摘除。",
        "value": WRITEBACK_REQUEST,
    },
    "03_remove_then_readd_uses_default": {
        "summary": "③ 删除后同名重加 → 沿用当前默认",
        "description": "上一轮写回删掉了 cache，重新读取后注解里只剩 db；"
        "这次把 cache 加回列表，响应中 cache 不带 retention，即跟随当前默认值 10。",
        "value": READD_REQUEST,
    },
    "04_missing_annotation_fails_batch": {
        "summary": "④ 丢注解写回 → 整批失败并指名",
        "description": "两个对象都带 resourceVersion，第二个缺兼容注解：整个 "
        "ConversionReview 返回 Failure 并指出问题对象，convertedObjects 为空——"
        "第一个合法对象也不会被转换。",
        "value": MISSING_ANNOTATION_REQUEST,
    },
    "05_corrupted_annotation_fails": {
        "summary": "⑤ 注解损坏 → 整批失败并指名",
        "description": "兼容注解不是合法 JSON，返回 Failure 并指出问题对象与原因。",
        "value": CORRUPTED_ANNOTATION_REQUEST,
    },
    "06_create_via_v1alpha1": {
        "summary": "⑥ 旧客户端新建（无 resourceVersion，免注解）",
        "description": "新建请求没有 resourceVersion，允许不带兼容注解；"
        "所有目标都不设单独份数，跟随默认。",
        "value": CREATE_REQUEST,
    },
}
