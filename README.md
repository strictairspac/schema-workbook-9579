# 实验委托模型拆解册

南麓材料实验室（9人）把委托单上的业务依赖整理为可验证的3NF关系，并保留候选键、闭包、最小覆盖和无损校验证据。

## 业务工作流

原先设计员在Excel里堆叠委托号、客户、检测项、方法与结果，再手工画表结构。痛点：依赖口径难确认、复合键容易漏判、拆表后无法证明无损。上游为委托单和已确认业务规则，本项目承接关系草案登记、函数依赖编辑、3NF合成、验证报告导出，下游为实验数据录入系统的数据库设计人员。

核心实体：关系草案 Relation、函数依赖 Dependency、分解报告 Report。4业务页和独立登录页；管理员另有审计页。业务规则不执行为SQL，属性名是纯数据。

## 快速启动

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 http://localhost:18243 ，后端 http://localhost:19243/docs ，MySQL localhost:17743。

|角色|账号|密码|权限|
|---|---|---|---|
|admin|admin@nanlu.test|Demo2026!|草案/依赖写入、运行、审计|
|operator|operator@nanlu.test|Demo2026!|草案/依赖写入、运行、导出|
|viewer|viewer@nanlu.test|Demo2026!|只读与导出，写API403|

## 一天的工作剧本

09:00 operator登录；09:05打开 `NL-1117-A` 材料检测委托单；09:10查看五属性 `委托号,客户,检测项,方法,结果`；09:15确认 `委托号→客户`、`检测项→方法`、`委托号,检测项→结果`；09:20执行 `shift-20261117` 分解任务；09:25候选键为 `{委托号,检测项}`，拆出委托客户、检测项方法、委托检测结果3个关系；09:30导出 `report-1.json`；09:40 viewer复核3NF/依赖保持/无损均通过；10:00 admin审计。

另有 `NL-1117-B` 的循环依赖 A→B/B→A与独立属性C，候选键AC/BC；`NL-1117-C` 没有确认依赖，完整属性集作为唯一候选键，不凭空猜测业务规则。

## 算法说明

Python真算，MySQL保存草案/依赖与结果，Redis传递任务，独立worker执行。

1. 属性闭包：迭代应用所有X→a，直到不再增加属性。
2. 候选键：先取不出现在RHS的必需属性，再按大小枚举其余属性子集；若闭包覆盖全集且不含已知候选键，加入结果。**最多10个属性/64条依赖**，最多1024个子集；超限API422/算法ValueError，避免指数搜索失控。
3. 最小覆盖：RHS拆成单属性，去平凡依赖；逐一删除左侧多余属性、移除冗余依赖。
4. 3NF合成：按最小覆盖左侧分组，删除被包含关系；若无关系含候选键，补入候选键关系。无依赖时保留全属性关系。
5. 验证：精确生成F+在各分解关系上的投影，枚举局部候选键，逐非平凡投影依赖检查3NF；所有原始依赖须可由投影并集推出。tableau追赶法依据FD合并符号，出现全a符号行则无损，报告包含见证行、合并步骤与最终表。

复合、循环、空决定项、无依赖均支持。不在任何RHS中的属性强制进入候选键。输入入队时冻结快照，后续修改不会改变已有报告。结果JSON含SHA256，固定输入应有固定输出。失败态包括引用关系外属性、重复/空属性、属性超过10、依赖超过64、队列错误；页面可恢复并重新执行。

这是小型关系模型设计辅助工具，FD由人工提供，不能从脏数据自动推断业务规则；不连接生产数据或自动执行DDL。精确投影在10属性内有界，仍可能随依赖复杂度增加耗时。单worker串行队列，不带生产任务重试/崩溃续跑。

## 依赖成员行迁移

函数依赖原先以 JSON 数组存两侧属性。迁移把每条规则拆成可按属性查找的成员行（`dependency_members`，每行一个属性、标明决定项/被决定项），规则本身仍保留旧列。

- **双写**：新建或修改依赖时，规则两侧与成员行在同一事务、同一次编辑中写入，任何一步失败整体回滚；同一规则的成员行共享内容摘要 `edit_token`，核对会拒绝两侧来自不同编辑的成员。
- **空决定项**：左侧为空时不产生决定项成员，但被决定项成员照常存在，规则仍可完整重建。
- **回填**：从 `migration_state.cursor` 起分批复制旧规则，逐条提交后才推进游标；已匹配的规则跳过，因此中断后可从原进度续跑，重复执行不会重复落成员。回填逐条加行锁，旧版编辑要么先落库、要么等复制完成。
- **核对**：只读比较每条规则与其成员行（缺失、多余、顺序、摘要、所属关系），不锁旧编辑器，核对期间旧版仍可改规则。
- **切换**：核对一致后管理员才能切换；切换只改变读取路径，新受理的分解报告才从成员行重建依赖，此前报告继续用入队时冻结的快照。核对不一致时切换返回 409，报告入队也会拒绝成员行不完整的草案。
- **回退**：管理员可回退到旧读法，报告改读旧依赖列；由于双写，切换后修改过的规则在旧列里仍是最新值。
- **页面**：侧栏与「迁移状态」页显示迁移中 / 已切换 / 已回退，并提供回填、核对、切换、回退、续跑与按属性查询成员行。

接口：`GET /migration`、`POST /migration/backfill?limit=50`、`POST /migration/verify`、`POST /migration/switch`、`POST /migration/rollback`、`POST /migration/resume`、`GET /dependencies/members?attribute=…&side=…&relation_id=…`。回填/续跑需写入角色，切换/回退需管理员。测试：`cd backend && python -m pytest tests`。

## 工程结构

|服务|技术|职责|
|---|---|---|
|frontend|原生HTML/CSS/JS + Nginx|4业务页、登录、迁移状态页、共享业务表格与报告组件|
|backend|Python3.11/FastAPI/SQLAlchemy/PyJWT|校验、权限、审计、快照与导出|
|worker|Python3.11|闭包、候选键、3NF与chase|
|db|MySQL8.0|JSON属性与依赖、任务报告|
|redis|Redis7|持久任务队列|

目录：`frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}`；`backend/src/{routes,controllers,services,models,schemas,middlewares,utils,constants,config}`；`backend/scripts/seed_demo.py`；`backend/migrations/001_initial.sql`；`backend/tests`。每核心实体贯穿模型/schema/service/route/API/store/page。组件table/badge/frame复用，hooks/session/feedback和utils/forms/html共用；request_id/errors双后端中间件。成员行迁移横跨`services/members.py`、`services/migration.py`与`routes/migration.py`。

枚举：角色admin/operator/viewer、任务queued/running/completed/failed；前后端定义位于 `backend/src/constants/enums.py` 和 `frontend/src/constants/enums.js`，security/reports、frame/badge消费；worker持久化状态。

环境变量见.env.example：COMPOSE_PROJECT_NAME、FRONTEND_PORT/BACKEND_PORT/DB_PORT、DB_PASSWORD、JWT_SECRET、SEED_DEMO。默认密码仅本地演示，JWT8小时。seed按自然键只补缺失关系及初始依赖，不覆盖用户修改。schema由SQLAlchemy metadata.create_all初始化，无在线迁移引擎。

本地开发：Python3.11安装backend/requirements.txt，配置DATABASE_URL/REDIS_URL后从backend运行 `uvicorn src.main:app --reload`、`python -m src.worker`；前端用Nginx同源代理/api。5服务均有健康检查和健康依赖，数据库/Redis/worker产物为独立命名卷。停止清空：`docker compose down -v --remove-orphans`。

UI为酒红档案册，双字体由Google Fonts加载，离线有回退且业务不依赖字体网络。

## License

MIT。真实测试脚本、截图与结果保存在源码外 `../../test-artifacts/gb-4104/`。
