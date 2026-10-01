# 荧光通道解混平台

面向两万像素见方、多通道荧光显微图像的版本化非负解混平台。系统把 **原图、控制矩阵、算法、整图冻结统计、金字塔层级、切片坐标、视图类型** 全部纳入缓存和发布校验；计算中换矩阵、取消后重试或迟到任务都不会把旧系数/旧代次切片拼进当前结果。

## 架构

- Angular 18 + OpenSeadragon：原通道、估计成分、重建残差三视图同屏联动。
- FastAPI：认证、RBAC、图像/矩阵/任务/结果 API、DZI JSON 与 PNG 切片。
- NumPy + SciPy：整图统计冻结、`optimize.nnls` 非负解混；另提供确定性 FISTA NNLS。
- MinIO（或本地文件后端）：原图、内容寻址派生切片、矩阵、发布报告；禁止原地覆盖。
- PostgreSQL：控制样本、矩阵版本、图像权限、任务代次、切片状态、结果版和审计日志。
- Celery + Redis：启动任务和按切片并行计算。

## 启动

```bash
docker compose up --build
# 前端 http://localhost:8080
# API  http://localhost:8000/docs
# MinIO http://localhost:9001  unmix/unmix-dev-secret
```

进入页面后点击“初始化演示数据”，再选择角色登录：

| 角色 | 账号 | 密码 |
|---|---|---|
| 仅查看原图 | `viewer@example.com` | `viewer-password` |
| 工程师 | `engineer@example.com` | `engineer-password` |
| 发布人 | `publisher@example.com` | `publisher-password` |
| 管理员 | `admin@example.com` | `admin-password` |

本地开发：

```bash
cd backend && pip install -r requirements.txt
uvicorn app.main:app --reload

cd frontend && npm install && npm start
```

测试（在 `backend/`）：

```bash
pytest -q
```

## 数据与版本规则

### 不可变输入

- `Image.source_digest` 由完整金字塔每个切片的摘要构成，不只是上传文件名或归档摘要。
- `MatrixVersion.coefficient_object_key` 与 `coefficient_digest` 内容寻址；已发布矩阵不可更新，只能新建版本。
- MinIO key 已存在时执行 create-only 写入，冲突直接报 `ImmutableObjectError`。
- 任务创建时记录：
  - `image_digest`、`source_object_key`
  - `matrix_digest`、`matrix_object_key`
  - `algorithm`、`algorithm_params`
- 启动时再次比对并锁定二者摘要；不一致则拒绝启动。

### 先冻结，后并行

`start_job` 在 fan-out 前读取所有 level-zero 原图切片，统一计算并冻结：

- 每通道暗电平 offset（稳健低分位数）
- 每通道 scale
- 饱和阈值与像素总数
- 图像/矩阵摘要和矩阵条件数

冻结载荷生成 `frozen_digest`。Worker 只消费冻结参数，不允许在单块中自估 offset/scale，因此不会因分块边界或调度顺序产生“伪统一模型”。

### 缓存键

派生切片缓存键包含：

```text
image digest | matrix digest | algorithm | algorithm params | frozen digest | level | x | y | kind
```

其中 `kind` 为 `component` 或 `residual`。矩阵发布新版本会得到不同 digest 和完整新命名空间；浏览器、后端对象和报告不会复用旧系数对象。旧任务迟到时，行状态写为 `stale`，不会进入当前代次。

## 任务代次

- 初始启动生成 `JobGeneration g1`。
- 取消会把当前代次标为 `cancelled`。
- 重试生成 `g2`，旧代次立即失效。
- 计算中矩阵换版只影响后续新任务；进行中任务仍绑定旧 `matrix_digest`。
- Worker 写入时在锁内检查：
  - `generation_no == job.current_generation_no`
  - `generation.status == active`
  - `job.cancel_requested == false`
- 不满足时输出 `stale` 或 `cancelled`，不会写派生对象为成功状态。

## 异常与预览

| 问题 | 记录方式 | 对发布影响 |
|---|---|---|
| 饱和 | 单块 `quality_flags.saturated/saturated_fraction`，任务汇总 `saturated_tiles` | 警告，不阻止（可按业务扩展） |
| 缺通道 | 矩阵校验 `missing_channel` 或单块校验 | 任务不可启动；单块缺通道为失败 |
| 矩阵病态 | 条件数超阈值，`ill_conditioned_matrix` | 任务不可启动 |
| 单块损坏/NaN | TileTask `failed`, `error_code=corrupt_tile` | 可预览明确部分结果；禁止发布 |
| 临时处理错误 | attempts 增加，最多 4 次后失败；`test_temporary_tile_failure_retries_until_attempt_limit` 覆盖 | 有失败块则禁止发布 |

前端用绿色/灰色/红色/紫色显示切片状态；部分结果有显著横幅。切片 HTTP 响应带：

- `X-Cache-Key`
- `X-Generation-No`
- `X-Image-Digest`
- `X-Unmixed: partial/current`

当前代次没有成功切片时 API 返回 409，而不是回退到旧矩阵切片。

## 权限和审计

`view_source`、`view_derived`、`publish_result` 分离：

- 能看原图不代表能计算或发布。
- 矩阵创建/发布要求 engineer/admin。
- 结果发布要求显式 `publish_result` 或 admin。
- 发布拒绝也写 `audit_logs`，动作是 `result.publish_denied`。
- 成功发布写 `result.publish`，包含发布人、图像、任务、代次、报告摘要和被替代结果。

正式发布在事务中锁定 job、当前 generation 和 image 行，并检查：

1. 当前代次仍是 active；
2. 所有必需 `component/residual` 任务均 `succeeded`；
3. 每个坐标的两个视图都来自同一个 generation；
4. 每块记录了 cache key 与 object digest；
5. 报告写入新的内容寻址 key；旧报告状态改为 `superseded`，原对象不覆盖。

竞争发布时，第一个事务发布成功，第二个收到 `409 concurrent_publication`。

## 验收场景脚本

### 1. 矩阵在计算中更新

1. 用 engineer/publisher 创建任务。
2. 任务运行中点击“模拟计算中换版”，或：
   ```bash
   POST /matrices { matrix_key, coefficients: 新系数, publish: true }
   ```
3. 观察旧任务仍完成于旧 digest；旧版本为 `superseded`，但任务的 `matrix_version_id/matrix_digest` 不变。
4. 再创建任务，新缓存命名空间和结果报告均使用新 digest。

对应测试：`tests/test_pipeline.py::test_matrix_mid_flight_keeps_job_on_old_digest`。

### 2. 一个切片反复失败

- 测试夹具把某个 level-zero 源块替换为含 NaN 的内容寻址坏块。
- 该块立即记录 `corrupt_tile`；其他块成功。
- Job 为 `partial`，预览显示坏块和警告，发布返回 `incomplete_generation`。

对应测试：`test_corrupt_single_tile_allows_preview_but_blocks_publish`。

### 3. 取消后重试

1. `POST /jobs/{id}/cancel` 使 g1 取消。
2. 任何迟到 worker 只把任务写为 `cancelled/stale`。
3. `POST /jobs/{id}/retry` 创建 g2；只有 g2 成功后才能发布。

对应测试：`test_cancel_then_retry_creates_deprecated_generation`；迟到取消任务的状态由同用例断言。

### 4. 两人竞争发布

- 发布路径对 image 行加锁；首个提交成为 published。
- 第二个提交收到 `concurrent_publication`，不会产生两个当前报告。
- viewer 无发布权限时返回 403 且记录拒绝审计。

对应测试：`test_viewer_cannot_publish_and_competing_publish_wins_once`。

### 5. 已知组分合成图

`synthetic_demo_image` 用已知三组分和四通道矩阵合成带噪声图；测试核对：

- level-zero 估计成分与真值 component RMSE；
- 单块 SciPy NNLS `tile_rmse`；
- reconstruction/residual 对象已持久化；
- 失败时不写 ResultVersion，不改变 source key/digest，不覆盖旧报告。

## 主要目录

```text
backend/app/
  main.py          FastAPI 路由
  models.py        PostgreSQL 模型
  services.py      版本、权限、代次、发布事务
  imaging.py       矩阵校验、冻结统计、NNLS、残差、PNG
  worker.py        单块计算与失败分类
  celery_app.py    Celery/Redis 调度
  storage.py       MinIO/本地不可变对象存储
  ingest.py        金字塔和合成数据
frontend/src/app/
  app.component.ts          控制台与状态网格
  linked-viewer.component.ts OpenSeadragon 三视图同步
  services/                 API、认证和自定义 digest TileSource
```
