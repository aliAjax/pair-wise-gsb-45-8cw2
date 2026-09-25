# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。靠泊计划创建后自动安排进出港拖轮护航，推力不够或时段冲突进入待配区，取消计划立即释放拖轮。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突和冲突检查。
- `src/tug_rules.py`：拖轮登记校验、最低马力计算、护航时间窗、选船与费用规则。
- `src/repository.py`：靠泊计划与审计的SQLite建表、事务和查询。
- `src/tug_repository.py`：拖轮、护航航段、派工任务的SQLite持久化。
- `src/service.py`：靠泊计划用例编排、权限检查、乐观并发和审计。
- `src/tug_service.py`：拖轮登记、护航派工、待配区与护航结算的用例编排。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：演示页面。
- `tests/`：完整流程、规则计算、拖轮派工和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 拖轮护航规则

- 最低马力 = `ceil(船长 × 12 × 风险系数)`，风险系数：low 1.0 / medium 1.25 / high 1.6；危险品船再乘 1.2。
- 护航时间窗：进港为`[eta, eta+2]`，出港为`[etd-2, etd]`，均限制在0-24时内。
- 选船：单艘拖轮可满足时取马力最小者，否则按马力从大到小累加，直至总推力达标。
- 拖轮可用时段必须完整覆盖护航时间窗；同一拖轮在重叠时段内不能同时接两船（派工事务内复核）。
- 推力不够或时段冲突的航段留在待配区并记录原因，补充拖轮或调整计划后可重新派工。
- 取消计划立即释放该计划全部未完成护航；护航结束后写入实际马力、时长和费用（费用 = 实际马力 × 实际时长 × 费率）。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/escorts`：该计划的进出港护航航段、派工拖轮与费用。
- `POST /api/records/{id}/escorts/retry`：对待配航段重新派工。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，创建后自动派工护航。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`；`cancel`会立即释放护航拖轮。
- `GET /api/tugs`：拖轮列表及其已派任务。
- `POST /api/tugs`：登记拖轮，请求体为`{"name":"...","horsepower_hp":3000,"available_from_hour":0,"available_to_hour":24,"rate_per_hp_hour":2.5}`。
- `GET /api/escorts/pending`：待配区列表，含计划、航段、时间窗、最低马力和待配原因。
- `POST /api/escorts/assignments/{id}/complete`：护航结束结算，请求体为`{"actual_hours":2,"actual_hp":3000}`，`actual_hp`缺省按登记马力。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。角色：`port_controller`管理靠泊计划，`tug_dispatcher`管理拖轮与护航（只读查看计划），`admin`拥有全部权限。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及拖轮派工（最低马力、总推力累加、时段冲突、待配原因、取消释放、护航结算）。
