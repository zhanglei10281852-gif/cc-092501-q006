# 地下水同位素与污染迁移计算服务

这是一个面向水文地质研究团队和环境监管人员的模块化后端，集中管理地下水井、同位素观测、补给端元、混合源反演、污染物迁移、计算任务、参数版本、结果置信区间、用户权限、会话和审计。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 井点与样本：登记井点坐标、含水层、采样批次和实验室测量结果。
- 同位素计算：处理稳定同位素、溶质浓度、检测限和质量守恒约束，反演多个补给端元比例。
- 污染迁移：计算一维平流、弥散和一阶衰减，提供到达时间和浓度曲线。
- 参数集版本：场地参数（孔隙率、流速、弥散、衰减、端元组成）按 `site_code + code` 分组版本化，经历草稿、复核、发布、撤销四个阶段；发布后内容不可变。
- 任务与审计：保存参数版本、计算输入摘要、置信区间、失败重试和结果差异。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 参数集生命周期

同一场地的孔隙率、流速和端元组成会随着新调查资料修订。参数集把一次修订固化为不可变版本，完整流程为：

```text
draft --submit--> in_review/pending --approve--> in_review/approved --publish--> published --retract--> retracted
                     └----reject----> draft/rejected（修改后可重新提交）
```

- 草稿可编辑；提交复核后内容冻结，发布时校验内容摘要（`content_digest`）防止篡改。
- 提交人与复核人不能是同一人；发布与撤销需要 `hydro.params.publish` 权限。
- 所有反演与迁移任务必须引用**已发布**的参数集版本（`parameter_set_id`），任务入队时把参数与端元快照整体写入 `input_json`，重跑历史任务永远使用当时的参数。
- 发布新版本只把引用旧版本的结果标记为 `stale`（`stale_reason` / `stale_since`），绝不改写历史 `result_json`；撤销已发布版本同理。
- 发布需携带 `expected_base_version_id`：若该分组已被并发发布了更新的版本，返回 409，可 `rebase` 到最新版本后重新走复核流程。
- `POST /api/hydro/param-sets/{id}/recompute` 按最新发布版本批量重算受影响结果：旧任务保留并挂上 `superseded_by_task_id`，新任务记录 `recompute_of_task_id`，重算幂等可重复执行。
- 前后差异：`GET /api/hydro/param-sets/diff?old=&new=` 给出参数与端元级差异；`GET /api/hydro/results/diff?task_type=&old=&new=` 给出反演比例、RMSE、峰值浓度、到达时间等结果级差异；`GET /api/hydro/param-sets/{id}/affected` 列出被标记的结果。
- 每次状态变更写入 `audit_events`（`hydro.params.*`）并记录复核轨迹；越权访问记录 `hydro.access.denied`（outcome=denied）。

新增权限：`hydro.read`、`hydro.write`、`hydro.params.write`、`hydro.params.review`、`hydro.params.publish`、`hydro.recompute`，管理员角色自动获得。

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

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、井点样本、同位素约束、迁移计算、任务恢复和数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  hydro/           地下水、同位素反演、污染迁移与参数集版本服务
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
