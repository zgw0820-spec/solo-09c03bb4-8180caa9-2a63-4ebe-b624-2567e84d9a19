"""BackupPolicy v1alpha1 <-> v1beta1 转换逻辑（纯函数、无状态）。

版本模型
--------
v1beta1（存储版本）::

    spec:
      defaultRetention: 3            # 默认保留份数
      targets:                       # 目标列表（按 name 唯一）
        - name: db
          retention: 5               # 可选：该目标单独的保留份数
        - name: logs                 # 未设置 -> 跟随 defaultRetention

v1alpha1（旧版，值班员的旧工具只认它）::

    spec:
      defaultRetention: 3
      targets: ["db", "logs"]        # 只有目标名列表

往返保真（round-trip）方案
------------------------
v1alpha1 表达不了"按目标单独设置份数"，因此：

* v1beta1 -> v1alpha1（旧客户端读取）：把所有 **显式** 单独设置汇总成 JSON，
  写进注解 ``backup.example.com/v1alpha1-compat``。注意：即使某目标的单独份数
  恰好等于当前默认值也照样记录——"显式设置"这个事实本身必须保留，否则旧客户端
  改默认值后，该目标会被错误地变成"跟随默认"。
* v1alpha1 -> v1beta1（旧客户端写回）：按目标名从注解恢复单独设置；注解里
  没有的目标（新增的，或删除后又同名加回的）不设置单独份数，即沿用当前默认值。
  兼容注解在写回时被摘除，不进入存储版本。

webhook 不保存任何状态：往返所需的全部信息都在注解里，
所以两次转换之间重启 webhook 进程不影响写回。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

GROUP = "backup.example.com"
KIND = "BackupPolicy"
V1ALPHA1 = "v1alpha1"
V1BETA1 = "v1beta1"
SUPPORTED_VERSIONS = (V1ALPHA1, V1BETA1)

#: 兼容注解：v1alpha1 视图上携带的、v1beta1 显式单独设置的完整映射。
COMPAT_ANNOTATION = f"{GROUP}/v1alpha1-compat"
#: 注解 payload 的版本号，便于将来演进时识别旧格式。
ANNOTATION_PAYLOAD_VERSION = 1


class ObjectConversionError(Exception):
    """单个对象转换失败（消息面向调用方，需能定位问题）。"""


@dataclass
class BatchConversionError(Exception):
    """整批转换失败：ConversionReview 是原子操作，任一对象失败则整批失败。"""

    problems: list[str] = field(default_factory=list)
    total: int = 0

    def __str__(self) -> str:  # pragma: no cover - 简单拼接
        return (
            f"conversion failed for {len(self.problems)} of {self.total} object(s): "
            + "; ".join(self.problems)
        )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def convert_objects(objects: list, desired_version: str) -> list[dict]:
    """把 objects 全部转换到 desired_version。

    任一对象失败都会累积起来，最后以 BatchConversionError 一次性抛出，
    消息里逐个指出问题对象（序号 + namespace/name + 原因）。
    """
    converted: list[dict] = []
    problems: list[str] = []
    for index, obj in enumerate(objects):
        try:
            converted.append(convert_object(obj, desired_version))
        except ObjectConversionError as exc:
            problems.append(f"[{index}] {_identity(obj)}: {exc}")
    if problems:
        raise BatchConversionError(problems=problems, total=len(objects))
    return converted


def convert_object(obj: dict, desired_version: str) -> dict:
    """转换单个对象；已在目标版本的对象原样透传。"""
    if not isinstance(obj, dict):
        raise ObjectConversionError("object is not a JSON object")
    kind = obj.get("kind")
    if kind != KIND:
        raise ObjectConversionError(f"unsupported kind {kind!r} (expected {KIND!r})")
    api_version = obj.get("apiVersion")
    prefix = f"{GROUP}/"
    if not isinstance(api_version, str) or not api_version.startswith(prefix):
        raise ObjectConversionError(f"unsupported apiVersion {api_version!r}")
    source_version = api_version[len(prefix):]
    if source_version not in SUPPORTED_VERSIONS:
        raise ObjectConversionError(f"unsupported apiVersion {api_version!r}")
    if source_version == desired_version:
        return obj
    if desired_version == V1ALPHA1:
        return _to_v1alpha1(obj)
    return _to_v1beta1(obj)


# ---------------------------------------------------------------------------
# v1beta1 -> v1alpha1（旧客户端读取）
# ---------------------------------------------------------------------------

def _to_v1alpha1(obj: dict) -> dict:
    spec = _spec_of(obj)
    default_retention = _read_retention(spec.get("defaultRetention"), "spec.defaultRetention")
    names: list[str] = []
    explicit: dict[str, int] = {}
    seen: set[str] = set()
    for i, entry in enumerate(_targets_of(spec)):
        if not isinstance(entry, dict):
            raise ObjectConversionError(f"spec.targets[{i}] must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ObjectConversionError(f"spec.targets[{i}].name must be a non-empty string")
        if name in seen:
            raise ObjectConversionError(f'duplicate target name {name!r} in spec.targets')
        seen.add(name)
        names.append(name)
        # 关键：只要 v1beta1 上显式带了 retention 就记录——哪怕数值等于默认值。
        if "retention" in entry and entry["retention"] is not None:
            explicit[name] = _read_retention(entry["retention"], f"spec.targets[{i}].retention")

    payload = json.dumps(
        {"v": ANNOTATION_PAYLOAD_VERSION, "retentions": explicit},
        sort_keys=True,  # 确定性输出，保证转换幂等（重复转换结果逐字节一致）
        separators=(",", ":"),
    )

    def fill(annotations: dict) -> None:
        annotations[COMPAT_ANNOTATION] = payload

    new_spec: dict = {}
    if default_retention is not None:
        new_spec["defaultRetention"] = default_retention
    new_spec["targets"] = names
    return _rebuild(obj, V1ALPHA1, new_spec, fill)


# ---------------------------------------------------------------------------
# v1alpha1 -> v1beta1（旧客户端写回 / 新建）
# ---------------------------------------------------------------------------

def _to_v1beta1(obj: dict) -> dict:
    meta = obj.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    annotations = meta.get("annotations")
    annotations = annotations if isinstance(annotations, dict) else {}
    raw = annotations.get(COMPAT_ANNOTATION)
    is_update = bool(meta.get("resourceVersion"))  # 新建请求没有 resourceVersion

    if raw is None:
        if is_update:
            raise ObjectConversionError(
                f'missing compatibility annotation "{COMPAT_ANNOTATION}" on a v1alpha1 '
                "update (object carries a resourceVersion); re-read the object through "
                "the v1alpha1 API before writing it back"
            )
        explicit: dict[str, int] = {}  # 旧版新建：没有注解是合法的
    else:
        explicit = parse_compat_annotation(raw)

    spec = _spec_of(obj)
    default_retention = _read_retention(spec.get("defaultRetention"), "spec.defaultRetention")
    targets: list[dict] = []
    seen: set[str] = set()
    for i, name in enumerate(_targets_of(spec)):
        if not isinstance(name, str) or not name:
            raise ObjectConversionError(f"spec.targets[{i}] must be a non-empty string")
        if name in seen:
            raise ObjectConversionError(f'duplicate target name {name!r} in spec.targets')
        seen.add(name)
        entry: dict = {"name": name}
        # 按名字恢复单独设置；注解里没有的（新增 / 删后重加）-> 不设置，跟随当前默认。
        if name in explicit:
            entry["retention"] = explicit[name]
        targets.append(entry)

    def strip(annotations: dict) -> None:
        annotations.pop(COMPAT_ANNOTATION, None)

    new_spec = {}
    if default_retention is not None:
        new_spec["defaultRetention"] = default_retention
    new_spec["targets"] = targets
    return _rebuild(obj, V1BETA1, new_spec, strip)


def parse_compat_annotation(raw: object) -> dict[str, int]:
    """解析并校验兼容注解；任何损坏都抛 ObjectConversionError。"""
    if not isinstance(raw, str):
        raise ObjectConversionError(
            f'corrupted compatibility annotation "{COMPAT_ANNOTATION}": value must be a string'
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ObjectConversionError(
            f'corrupted compatibility annotation "{COMPAT_ANNOTATION}": '
            f"invalid JSON ({exc.msg})"
        ) from None
    if not isinstance(data, dict):
        raise ObjectConversionError(
            f'corrupted compatibility annotation "{COMPAT_ANNOTATION}": expected a JSON object'
        )
    version = data.get("v")
    if version != ANNOTATION_PAYLOAD_VERSION:
        raise ObjectConversionError(
            f'corrupted compatibility annotation "{COMPAT_ANNOTATION}": '
            f"unsupported payload version {version!r} (expected {ANNOTATION_PAYLOAD_VERSION})"
        )
    retentions = data.get("retentions")
    if not isinstance(retentions, dict):
        raise ObjectConversionError(
            f'corrupted compatibility annotation "{COMPAT_ANNOTATION}": '
            '"retentions" must be an object mapping target name to a non-negative integer'
        )
    out: dict[str, int] = {}
    for name, value in retentions.items():
        if not isinstance(name, str) or not name:
            raise ObjectConversionError(
                f'corrupted compatibility annotation "{COMPAT_ANNOTATION}": '
                "retention keys must be non-empty strings"
            )
        try:
            out[name] = _read_retention(value, f"retention for target {name!r}")
        except ObjectConversionError as exc:
            raise ObjectConversionError(
                f'corrupted compatibility annotation "{COMPAT_ANNOTATION}": {exc}'
            ) from None
        if out[name] is None:  # JSON null 不是合法的份数
            raise ObjectConversionError(
                f'corrupted compatibility annotation "{COMPAT_ANNOTATION}": '
                f"retention for target {name!r} must be a non-negative integer, got null"
            )
    return out


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _rebuild(obj: dict, version: str, new_spec: dict, mutate_annotations) -> dict:
    """以原对象为基础构造目标版本对象：保留全部 metadata 与 status，只改注解。"""
    meta = obj.get("metadata")
    meta = dict(meta) if isinstance(meta, dict) else {}
    annotations = meta.get("annotations")
    annotations = dict(annotations) if isinstance(annotations, dict) else {}
    mutate_annotations(annotations)
    if annotations:
        meta["annotations"] = annotations
    else:
        meta.pop("annotations", None)
    out = {
        "apiVersion": f"{GROUP}/{version}",
        "kind": KIND,
        "metadata": meta,
        "spec": new_spec,
    }
    if "status" in obj:
        out["status"] = obj["status"]
    return out


def _spec_of(obj: dict) -> dict:
    spec = obj.get("spec")
    if spec is None:
        return {}
    if not isinstance(spec, dict):
        raise ObjectConversionError("spec must be an object")
    return spec


def _targets_of(spec: dict) -> list:
    targets = spec.get("targets")
    if targets is None:
        return []
    if not isinstance(targets, list):
        raise ObjectConversionError("spec.targets must be a list")
    return targets


def _read_retention(value: object, where: str) -> int | None:
    """读取保留份数：None 原样返回；其余必须是非负整数（bool 不算）。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ObjectConversionError(
            f"{where} must be a non-negative integer, got {value!r}"
        )
    return value


def _identity(obj: object) -> str:
    """用于报错信息定位对象：BackupPolicy "namespace/name"。"""
    if not isinstance(obj, dict):
        return f"{KIND} <not-a-json-object>"
    meta = obj.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    name = meta.get("name") or "<unnamed>"
    namespace = meta.get("namespace")
    if namespace:
        return f'{KIND} "{namespace}/{name}"'
    return f'{KIND} "{name}"'
