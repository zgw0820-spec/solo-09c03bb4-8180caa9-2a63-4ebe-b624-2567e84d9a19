# BackupPolicy Conversion Webhook

Kubernetes CRD `backuppolicies.backup.example.com` 的版本转换 webhook（FastAPI 实现）。
集群升级备份控制器后，**v1beta1 是存储版本**，支持为每个备份目标单独设置保留份数；
值班员的旧工具仍只认 **v1alpha1**（只有默认保留份数 + 目标名列表）。本 webhook 让
旧工具在不了解新字段的情况下安全地读、改、写回，且不丢任何 per-target 设置。

## 版本模型

```yaml
# v1beta1（存储版本）
spec:
  defaultRetention: 3
  targets:
    - name: db
      retention: 5        # 可选：单独份数；缺省 = 跟随 defaultRetention
    - name: logs

# v1alpha1（旧版）
spec:
  defaultRetention: 3
  targets: ["db", "logs"]
```

## 转换契约（实现要点）

v1alpha1 表达不了 per-target 设置，往返保真靠对象上的**兼容注解**
`backup.example.com/v1alpha1-compat` 完成：

- **读（v1beta1 → v1alpha1）**：把所有**显式**单独设置汇总成 JSON 写进注解，
  如 `{"retentions":{"cache":3,"db":5},"v":1}`。即使某目标的份数恰好等于当前
  默认值也照样记录——"显式设置"这个事实必须保留，否则旧客户端改默认值后它会
  被错误地变成"跟随默认"。
- **写回（v1alpha1 → v1beta1）**：按**目标名**从注解恢复单独设置；注解里没有的
  目标（新增的、或删除后又同名加回的）不设置份数，即**沿用当前默认值**。注解在
  写回时被摘除，不进入存储版本。目标删除后再同名添加会丢设置，是因为每次读取
  都按存储对象重新生成注解——删过的目标自然不在里面。
- **无状态**：往返所需的全部信息都在注解里，两次转换之间重启 webhook 不影响写回。
- **失败语义**：
  - 旧版**新建**（无 `resourceVersion`）：免注解，正常转换；
  - 带 `resourceVersion` **写回**时注解**缺失或损坏**：整个 ConversionReview
    返回 `Failure`（HTTP 200 + `response.result`），`convertedObjects` 为空，
    消息逐个指出问题对象（序号 + `namespace/name` + 原因）——批内合法对象也
    不会被转换，保证整批原子性；
  - 注解损坏判定：非法 JSON、payload 版本不符、`retentions` 非对象、份数为
    负数/非整数/布尔等；
  - 附带防御性校验：目标名重复、类型错误等同样整批失败。

## 目录

```
app/conversion.py   转换逻辑（纯函数、无状态）
app/main.py         FastAPI 协议层：POST /convert、GET /healthz
app/examples.py     Swagger 演示 / 测试共用的示例载荷
manifests/crd-backuppolicy.yaml      CRD（v1beta1 存储、webhook 转换）
manifests/webhook-deployment.yaml    可选：真实集群部署 webhook
scripts/demo.py     对运行中的服务实测全流程
tests/              pytest（25 例）
Dockerfile / docker-compose.yaml     本地一键启动
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8080/docs
```

在 `POST /convert` 的 **Example** 下拉框里按编号演示"旧客户端读 → 改 → 写回"：

1. **① 读取**：v1beta1 → v1alpha1，per-target 设置收进兼容注解
   （`cache` 的 3 恰好等于默认值，也照样记录）；
2. **② 写回：改默认值 + 重排目标**：默认值 3→10、目标重排为 `[logs, cache, db]`，
   响应中 `db=5`、`cache=3` 按名字恢复为显式设置（`cache` 不跟随新默认 10），
   注解被摘除；
3. **③ 删除后同名重加**：`cache` 被删过一轮再加回 → 沿用当前默认，不带份数；
4. **④ 丢注解 / ⑤ 注解损坏**：带 resourceVersion 写回 → 整批 Failure 并指名；
5. **⑥ 旧客户端新建**：无 resourceVersion，免注解。

**重启验证**：执行①后 `docker compose restart webhook`，再执行②，结果不变。

不用 Docker 也可以本地跑：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --port 8080
.venv/bin/python scripts/demo.py     # 实测全流程
.venv/bin/python -m pytest           # 25 个测试
```

## 部署到真实集群

```bash
kubectl apply -f manifests/crd-backuppolicy.yaml        # caBundle 见下
kubectl apply -f manifests/webhook-deployment.yaml      # 可选：webhook 本体
```

- `spec.conversion.webhook.clientConfig.caBundle` 需填入签发 webhook 证书的 CA
  （建议 cert-manager，CRD 顶部有注入注释）；Compose 演示不经过 API server，用不到。
- webhook 以 HTTPS 暴露 `/convert`（部署清单里 uvicorn 直接持证书，Service 443→8443）。

## 自测结果（本仓库交付前实测）

- `pytest`：**25 passed**。覆盖：读取收注解（含"等于默认也记录"）、改默认值 +
  重排写回、增删目标、删后同名重加跟随当前默认、重启后写回、丢注解/注解损坏
  （8 种变体）整批失败并指名、旧版新建（无注解/带合法注解/带坏注解）、重复目标
  拒绝、往返幂等、同版本透传、未知版本拒绝、uid 回显、健康检查。
- 实跑 `uvicorn` + `scripts/demo.py`：读 → 改默认值 3→10 + 重排 → 写回 →
  删 cache 写回 → 重读 → 同名加回 → 跟随默认；丢注解写回返回
  `conversion failed for 1 of 2 object(s): [1] BackupPolicy "default/web-backup":
  missing compatibility annotation ...`，合法对象不被转换。**全部断言通过**。
- **重启实测**：读取后 `kill` 掉 uvicorn 再重启，用重启前拿到的视图写回，
  转换成功且结果正确。
- `docker-compose.yaml`、两份 manifest 均通过 YAML 解析校验；CRD 的
  served/storage 与 webhook 路径断言正确。

## 未完成 / 限制

- 本开发环境没有 Docker，`docker compose up` 未实际执行；Compose 场景已用等价的
  uvicorn 直跑（含进程重启）验证，Compose/Dockerfile 经静态校验与人工核对。
- 未在真实 Kubernetes 集群做端到端验证（需 API server 回调 + TLS 证书）；
  `caBundle` 是占位符，上集群前必须注入真实 CA。
- 旧客户端若绕过"先读后写"（如用静态文件 apply 覆盖已有对象），会因缺注解被
  拒绝——这是需求要求的行为，但上线前需告知值班员正确姿势。
- 新建请求若带合法注解会按注解恢复（方便克隆对象），带坏注解则拒绝；
  该行为是本实现的自定义取舍，需求未明确。
- 仅声明支持 ConversionReview `v1`（对 `v1beta1` 形状的请求宽容回显，未正式支持）。
