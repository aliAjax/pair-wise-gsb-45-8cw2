# 港口泊位与航道调度（含拖轮护航派工）

纯Python标准库实现的港口泊位、航道与拖轮护航调度原型，使用SQLite持久化，HTTP接口由`http.server`提供，无第三方依赖。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：靠泊计划状态转换、靠泊可行性、吃水安全、泊位时间窗、护航前置校验。
- `src/tug_rules.py`：拖轮派工纯规则——最低马力、护航时段、选轮/冲突判定、待配原因、费用。
- `src/repository.py`：SQLite建表、派工事务和查询（计划、拖轮、护航航段、占用关系、审计）。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：拖轮登记、待配区、护航全流程演示页面。
- `tests/`：完整流程、规则计算、派工与失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 拖轮派工规则

- **拖轮登记**：名称（唯一）、马力、单个可用时段`available_from_hour`~`available_to_hour`（0~24，整点）。
- **最低马力**：`船长(米) × 8`，危险品按IMDG等级乘系数（1/7类1.5、2/3/5/6类1.3、4类1.2、8类1.15、9类1.1），向上取整。
- **护航航段**：每条靠泊计划安排进港、出港两段，各占2小时窗（进港`[eta, eta+2)`，出港`[etd-2, etd)`，自动裁到0~24）。
- **选轮**：可用时段须完整覆盖护航窗；大马力优先凑足总推力；同一条拖轮同一窗口不能同时服务两船（派工在单个数据库事务内计算并落库，并发下靠乐观版本+行事务保证不重复占用）。
- **待配区**：推力不足（全队合计低于需求）或时段冲突（合计够但窗口内拖轮被占用）的航段留在`pending`，记录明确原因；新增/释放拖轮后可对同计划重排，已配齐/已结束的航段不重排。
- **联动**：靠泊前进港护航必须结束，离泊前出港护航必须结束。
- **取消即释放**：计划取消在同一事务内把其全部航段与占用置为`released`，拖轮可立即派给其他船。
- **护航结束**：写入实际马力、实际时长（不超过护航窗）和费用；费用 = 实际马力 × 时长 × 1.5元/(匹·小时)。结束后该拖轮即不再占用。

## 主要接口

计划（原有）：

- `GET /health`、`GET /`：健康检查、演示页面。
- `GET /api/records`（`state`、`limit`）、`GET /api/records/{id}`、`GET /api/records/{id}/audit`、`GET /api/stats`。
- `POST /api/records`：`{"reference":"...","data":{...}}`，危险品需带`dangerous_class`（1~9）。
- `POST /api/records/{id}/actions/{action}`：`confirm/berth/depart/cancel`，体为`{"expected_version":1,"data":{...}}`。

拖轮与护航（新增）：

- `POST /api/tugs`：登记拖轮，`{"data":{"name":"...","horsepower":4000,"available_from_hour":0,"available_to_hour":24}}`。
- `GET /api/tugs`：拖轮清单。
- `POST /api/records/{id}/escorts`：安排/重排进、出港护航，`{"expected_version":n}`；不足或冲突时对应航段留在待配区。
- `GET /api/escorts?status=pending`：护航看板（带拖轮清单），`status=pending`只看待配航段。
- `POST /api/records/{id}/escorts/inbound/complete`（或`outbound`）：`{"expected_version":n,"data":{"actual_horsepower":2000,"duration_hours":2}}`，返回含费用的最新计划。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。`port_controller`可登记拖轮并执行计划动作；`port_controller`与`tug_dispatcher`可安排/结束护航；`admin`拥有全部权限。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖完整护航流程与审计、最低马力/危险品系数、推力不足与时段冲突待配、并发不重复占用、取消立即释放、护航结束写实际马力/时长/费用、重复派工与版本冲突等。
