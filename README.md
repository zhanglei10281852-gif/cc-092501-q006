# 地下水同位素与污染迁移计算服务

这是一个面向水文地质研究团队和环境监管人员的模块化后端，集中管理地下水井、同位素观测、补给端元、混合源反演、污染物迁移、计算任务、参数版本、结果置信区间、用户权限、会话和审计。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 井点与样本：登记井点坐标、含水层、采样批次和实验室测量结果。
- 同位素计算：处理稳定同位素、溶质浓度、检测限和质量守恒约束，反演多个补给端元比例。
- 污染迁移：计算一维平流、弥散和一阶衰减，提供到达时间和浓度曲线。
- 参数集版本：同一场地的孔隙率、流速、弥散度和端元组成按"草稿 → 复核 → 发布 → 撤销"管理，发布版本带序号与内容哈希且不可变。
- 不可变引用：所有反演与迁移任务必须引用已发布参数集版本，并固化版本号与内容哈希；流速与弥散度直接取自参数集。
- 影响标记与重算：发布新版本只把引用旧版本的结果标记为 `affected`，不重写历史；用户可批量重算，系统生成新任务行并保存前后差异（端元比例、RMSE、峰值浓度、到达时间）。
- 并发控制：草稿修订用内容哈希做乐观锁；并发发布按基准版本检测冲突，需显式 `force` 才能覆盖。
- 任务与审计：保存参数版本、计算输入摘要、置信区间、失败重试和结果差异。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、井点样本、同位素约束、迁移计算、参数集草稿/复核/发布/撤销状态机、乐观锁与并发发布冲突、影响标记、批量重算差异、任务恢复和数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 参数集版本流程

同一场地的孔隙率、流速和端元组成会随新调查资料修订。参数集以井点 `code` 为场地标识，遵循以下规则：

- **状态机**：`draft → in_review → published → revoked`，复核可退回草稿（需填写意见），发布版只能撤销、不能修改；发布时分配场地内递增 `revision`，并计算 `content_hash`。
- **不可变引用**：反演（`POST /api/hydro/samples/{id}/inversions`）和迁移（`POST /api/hydro/wells/{id}/transport`）必须传 `parameter_set_id`，且只能引用 `published` 版本；任务行固化 `parameter_revision` 与 `parameter_hash`，迁移的 `velocity_m_day`、`dispersion_m2_day` 来自参数集。
- **发布只标记、不重写**：发布新版时，引用上一发布版的任务被标记为 `affected`（结果原文与版本引用均不变）；可通过 `GET /api/hydro/affected-results` 查看。
- **批量重算与差异**：`POST /api/hydro/parameter-sets/{id}/recalculate` 基于新版本生成**新任务行**（旧行置为 `recalculated` 并指向新行），差异记入 `hydro_result_diffs`，可通过 `GET /api/hydro/parameter-sets/{id}/diffs` 查看；该操作幂等，重复调用跳过已重算任务。
- **并发冲突检测**：草稿修订须回传 `base_hash` 做乐观锁；两个基于同一旧版的草稿并发发布时，后发布者收到 409 及当前发布版 ID，显式带 `?force=true` 才能覆盖。
- **权限与审计**：涉及 `hydro.parameters.read/draft/review/publish/revoke`、`hydro.sites.write`、`hydro.tasks.run`、`hydro.results.read` 共 9 项权限；每次草稿、提交、退回、发布、撤销、重算都写入 `audit_events`（含操作者、前后状态、失败原因）。

典型流程（需携带管理员或具备相应权限账号的 Bearer 令牌）：

```bash
# 1. 起草
curl -sS -X POST http://127.0.0.1:8432/api/hydro/sites/W-001/parameter-sets \
  -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
  -d '{"porosity":0.25,"velocity_m_day":2.0,"dispersion_m2_day":5.0,"endmembers":[{"code":"RAIN","name":"山区降水","isotope_d18o":-10,"isotope_d2h":-70,"solute_mg_l":10,"uncertainty":0.1},{"code":"RIVER","name":"河流渗漏","isotope_d18o":-5,"isotope_d2h":-35,"solute_mg_l":50,"uncertainty":0.2}]}'
# 2. 提交复核 -> 3. 发布
curl -sS -X POST http://127.0.0.1:8432/api/hydro/parameter-sets/1/publish \
  -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' -d '{"note":"2026 年调查修订"}'
# 4. 发布新版后查看受影响结果并批量重算（可按 kind 筛选）
curl -sS -X POST http://127.0.0.1:8432/api/hydro/parameter-sets/2/recalculate \
  -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' -d '{"kinds":["transport"]}'
# 5. 查看前后差异
curl -sS http://127.0.0.1:8432/api/hydro/parameter-sets/2/diffs -H 'Authorization: Bearer <token>'
```

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  hydro/            地下水、同位素反演和污染迁移服务
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
