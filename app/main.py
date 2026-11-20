"""BackupPolicy ConversionReview webhook 的 FastAPI HTTP 层。

只负责协议解包/打包与失败语义，转换规则全部在 app.conversion 里。
"""

from __future__ import annotations

import logging

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from .conversion import (
    GROUP,
    SUPPORTED_VERSIONS,
    BatchConversionError,
    convert_objects,
)
from .examples import OPENAPI_EXAMPLES

logger = logging.getLogger("backup-policy-conversion")

# 本 webhook 说 ConversionReview v1；对旧版 v1beta1 形状的请求也宽容回显。
_REVIEW_API_VERSIONS = {"apiextensions.k8s.io/v1", "apiextensions.k8s.io/v1beta1"}
_DEFAULT_REVIEW_API_VERSION = "apiextensions.k8s.io/v1"

app = FastAPI(
    title="BackupPolicy Conversion Webhook",
    version="1.0.0",
    description="""
Kubernetes CRD `backuppolicies.backup.example.com` 的版本转换 webhook：
**v1beta1 是存储版本**，v1alpha1 是值班员旧工具使用的旧版。

## 在 Swagger 里演示"旧客户端读 → 改 → 写回"

打开 `POST /convert`，在请求体的 **Example** 下拉框里按编号选择：

1. **① 旧客户端读取**：存储的 v1beta1 → v1alpha1，per-target 设置收进兼容注解
   （`cache` 的 3 恰好等于默认值，也照样记录）；
2. **② 写回：改默认值 + 重排目标**：默认值 3→10、目标重排，按名字恢复单独设置，
   `cache` 仍是显式 3（不会跟随新默认 10）；
3. **③ 删除后同名重加**：`cache` 被删过一轮再加回 → 沿用当前默认，不带单独份数；
4. **④ 丢注解 / ⑤ 注解损坏**：带 resourceVersion 写回 → 整批 Failure 并指出问题对象；
5. **⑥ 旧客户端新建**：无 resourceVersion，免注解，正常转换。

webhook 无状态：执行①后 `docker compose restart webhook` 再执行②，结果不变。
""",
)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/convert")
def convert(review: dict = Body(..., openapi_examples=OPENAPI_EXAMPLES)) -> JSONResponse:
    review_api_version = review.get("apiVersion")
    if review_api_version not in _REVIEW_API_VERSIONS:
        review_api_version = _DEFAULT_REVIEW_API_VERSION

    request = review.get("request")
    if not isinstance(request, dict):
        return _failure(None, review_api_version, 'malformed ConversionReview: missing "request" object')
    uid = request.get("uid")

    desired = request.get("desiredAPIVersion")
    prefix = f"{GROUP}/"
    if (
        not isinstance(desired, str)
        or not desired.startswith(prefix)
        or desired[len(prefix):] not in SUPPORTED_VERSIONS
    ):
        expected = ", ".join(f"{GROUP}/{v}" for v in SUPPORTED_VERSIONS)
        return _failure(uid, review_api_version, f"unsupported desiredAPIVersion {desired!r}; expected one of: {expected}")
    desired_version = desired[len(prefix):]

    objects = request.get("objects")
    if objects is None:
        objects = []
    if not isinstance(objects, list):
        return _failure(uid, review_api_version, 'malformed ConversionReview: "request.objects" must be a list')

    try:
        converted = convert_objects(objects, desired_version)
    except BatchConversionError as exc:
        # 整批失败：HTTP 200 + result.status=Failure，convertedObjects 缺省。
        logger.warning("conversion rejected: %s", exc)
        return _failure(uid, review_api_version, str(exc))

    return JSONResponse(
        {
            "apiVersion": review_api_version,
            "kind": "ConversionReview",
            "response": {
                "uid": uid,
                "result": {"status": "Success"},
                "convertedObjects": converted,
            },
        }
    )


def _failure(uid: object, review_api_version: str, message: str) -> JSONResponse:
    response: dict = {"result": {"status": "Failure", "message": message}}
    if uid is not None:
        response["uid"] = uid
    return JSONResponse(
        {
            "apiVersion": review_api_version,
            "kind": "ConversionReview",
            "response": response,
        }
    )
