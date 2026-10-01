# 荧光通道解混平台 (Fluorescence Channel Unmixing Platform)

两万像素级多通道显微图像的**非负串色解混**平台：FastAPI + NumPy/SciPy 计算、
OpenSeadragon 金字塔切片联动（原通道 / 估计成分 / 残差）、MinIO 内容寻址存储、
PostgreSQL 元数据、Celery/Redis 切片调度，Angular 前端。

## 核心一致性保证（本平台解决的问题）

> 串色矩阵一旦换版，浏览器缓存、后台切片和最终报告必须一起失效，不能出现
> 局部沿用旧系数的拼接结果。

| 风险 | 机制 |
|---|---|
| 计算中矩阵换版，部分切片用旧系数 | 任务**启动时固定图像与矩阵的 sha256 摘要**；矩阵发布只新增不可变版本（旧版本字节保留）。换版立即把在跑的任务代次围栏（`superseded`），迟到切片只落入废弃代次，并自动创建固定到新摘要的后继任务 |
| 各切片自行估计背景/尺度伪装成统一模型 | **整图统计参数先冻结**（背景、噪声、显示归一化、矩阵病态度量），冻结后产生内容哈希；每个切片只做逐像素 NNLS，并校验参数哈希 |
| 浏览器/对象存储缓存旧切片 | 切片缓存键嵌入 `图像摘要+矩阵摘要+算法+代次+层级`，响应 `Cache-Control: immutable`。矩阵换版 → URL/键命名空间整体改变，浏览器、切片、报告同时失效 |
| 单块损坏拖垮全图 | 切片独立、有界定次重试（默认 3 次）；永久失败的块进入 `failed`，代次为 `partial`，预览以明确占位图标识，**部分代次不可发布** |
| 失败覆盖原图/旧报告 | 原图、矩阵、报告全部**内容寻址、只增不覆盖**；同键异字节写入被拒绝；报告“当前指针”只在提交事务时移动 |
| 有查看权就能发布 | 查看原图（`can_view`）与发布结果（`can_publish` + `result:publish` 能力）分离；发布矩阵是独立能力 `matrix:publish`；所有发布尝试（允许/拒绝）写审计表 |
| 两人竞争发布同一任务 | 发布对任务行加锁 + 单任务唯一发布约束；后提交者得到 409 并被审计 |
| 正式发布混入缺片/跨代切片 | 发布闸：当前代次必须 `completed`，逐级核对切片网格数量，并校验每个对象键都在同一图像/矩阵/算法/代次命名空间内 |

## 目录

```
backend/
  app/
    main.py            FastAPI 入口
    models.py          PostgreSQL/SQLite ORM：图像/权限/控制样本/矩阵版本/任务/代次/切片/结果/审计
    unmix.py           非负解混：FISTA-NNLS（向量化，对照 SciPy NNLS 验证）、冻结参数、矩阵校验、恢复误差
    imaging.py         HWC 数组、均值金字塔、切片 PNG、已知组分合成图
    pipeline.py        freeze→fan-out→finalize 编排、代次围栏、取消/重试/换版后继
    release.py         发布闸（同代次完整性 + 清单校验 + 审计 + 竞争串行化）
    object_store.py    Local / MinIO 存储，内容寻址不可变对象
    tasks.py           EagerRunner（单机/测试）与 CeleryRunner（Redis）
    routers/           images / matrices / jobs / viewer / results / audit / 测试故障注入
  tests/               24 个验收测试
frontend/             Angular 18 + OpenSeadragon 4
docker-compose.yml     Postgres + MinIO + Redis + API + Celery worker + 前端 nginx
```

## 快速开始（单机演示，无需 Docker）

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate    # 或用系统 Python
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000        # 默认本地对象存储 + 进程内线程池
# 另一个终端：
cd frontend && npm install --legacy-peer-deps && npm start   # http://localhost:4200
```

预置 API Key（仅演示，生产用环境变量覆盖）：
`imager-key`、`analyst-key`、`matrixer-key`、`publisher-key`、`admin-key`。

典型流程：
1. imager 在“图像”页生成 1024² 已知组分合成图（带真值，用于核对恢复误差）；
2. imager 新建任务 → 平台冻结整图参数、并行切片，前端轮询三个联动视图；
3. matrixer 在“矩阵版本”页发布修订 → 在跑任务立即废弃、自动后继；
4. 单个切片反复失败时代次变为**部分结果**，预览明确标识且无法发布；
5. publisher（需要图像级 `can_publish` 授权）只能发布**完整同代次**结果；
6. admin 在 `/audit` 查看所有发布尝试。

## 生产部署

```bash
cp .env.example .env   # 修改所有密钥
docker compose up -d --build
```

Celery 横向扩 worker 即可按切片并行；冻结阶段（整图统计）始终只执行一次，
与 worker 数量无关。

## 验收场景与测试

```bash
cd backend && python3 -m pytest
```

| 验收场景 | 测试 |
|---|---|
| 矩阵在计算中换版 | `test_matrix_revision.py`：旧代次围栏、迟到切片废弃、后继固定新摘要、旧任务不可发布、新旧切片键零交集 |
| 一个切片反复失败 | `test_tile_failure.py`：有界定次重试 → `partial`，占位图带 `X-Unmix-Tile-State: failed` 且 `no-store`，发布被拒；邻块正常 |
| 取消后重试 | `test_cancel_retry.py`：取消围栏在飞切片；重试复用同一图像/矩阵摘要与冻结哈希，但开启全新代次 |
| 两人竞争发布 | `test_permissions.py::test_concurrent_publish_race_single_winner`：200 + 409，仅一条结果 |
| 已知组分合成图 | `test_happy_path.py`：恢复相对 RMSE < 20%（粗金字塔层）、重建残差 < 2% 满量程 |
| 饱和 / 缺通道 / 病态矩阵 | `test_data_conditions.py`：饱和比例进冻结参数；缺通道在冻结阶段硬失败；病态矩阵告警但可运行 |
| 失败不覆盖原图/旧报告 | `test_data_conditions.py`：原图字节前后一致；重复发布不改动首份报告 |
| 查看≠发布 | `test_permissions.py`：analyst / 仅 view 授权 / matrixer 全部 403，拒绝写审计 |

## 关键 API

- `POST /images/synthetic`（生成带真值合成图）、`POST /images/upload`（.npy HWC）
- `POST /control-samples`、`POST /matrices`、`POST /matrices/from-controls`
- `POST /jobs`（固定摘要）、`POST /jobs/{id}/cancel|retry`
- `GET  /jobs/{id}`（含各代次切片状态、冻结参数、RMSE）
- `GET  /viewer/jobs/{job}/generations/{gen}/{view}.dzi` 与 `…/{view}_files/{lvl}/{x}_{y}.png?layer=`
- `POST /results/release`（发布闸 + 审计）、`GET /results`
- `GET  /audit`（仅 admin/审计能力）

## 非负解混

逐像素求解 `min_X ‖X Mᵀ − Y‖² s.t. X ≥ 0`（FISTA，全像素向量化；
与 `scipy.optimize.nnls` 逐点结果误差 < 5e-7）。整图背景（0.5% 分位）、
噪声尺度（MAD）、显示归一化在冻结阶段由全图粗网格一次性估计并哈希固定。
