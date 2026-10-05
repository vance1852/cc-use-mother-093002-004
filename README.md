# 监管医疗智能应用上线协作基础服务

本项目提供跨境数字贸易合作业务共享的服务端基础能力，负责合作机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

`digital_trade_foundation.governance` 在此基础上实现了**医疗智能辅助应用（含模型）的上线、运行与事故恢复监管**：

- 统一登记应用/模型版本、适用科室与人群、验证数据摘要、风险分级、阈值、岗位权限、人工复核条件与停用预案；
- 技术验证、业务签署、安全审批由三个不同岗位按固定顺序会签，版本、阈值或适用范围任一变化都会改变配置指纹并令批准重新进入评估；
- 运行期把自动建议、人工决定（接管必须留原因且岗位有权接管）、关联任务和支撑证据只追加地连接起来；
- 事故按业务键语义去重（重复上报只形成一次处置），可暂停特定范围并持久化复核时限，追踪、复核、恢复只追加，恢复只解除暂停、不改写临床事实；
- 支持双向追溯：从事故向后查看每项处置与恢复依据，从任一版本向前列出当前授权范围、未闭环风险与可接管岗位。

## 目录

- src/digital_trade_foundation/：领域模型、SQLite 存储、权限服务、审计链、智能辅助监管域、HTTP 路由和离线验收；
- tests/：基础规则、监管规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m digital_trade_foundation.acceptance

验收命令在临时 SQLite 数据库中完成基础登记链，以及智能辅助应用的上线会签、运行连线、事故暂停与恢复全链路，核对幂等回执、事故去重、临床事实保留与审计链；成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m digital_trade_foundation.api --database digital_trade.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态、暂停范围、复核时限与审计历史继续保留。

### 监管域接口

写入均为 POST，携带 X-Actor-Id 与 request_id（幂等）：

| 接口 | 说明 |
| --- | --- |
| /ai/applications | 登记应用或模型产品 |
| /ai/versions | 登记不可变版本（验证数据摘要、风险分级） |
| /ai/deployments | 在科室+人群上建立部署（阈值、复核条件、接管岗位、停用预案） |
| /ai/deployments/config | 修改阈值/范围等配置；配置指纹变化，批准回到待评估 |
| /ai/appraisals | 三方会签：technical → clinical → security，分属不同岗位 |
| /ai/recommendations | 记录自动建议（仅在已授权且未暂停的范围内） |
| /ai/decisions | 记录人工决定；接管必须填写原因，且岗位在接管范围内 |
| /ai/related-tasks | 关联任务 |
| /ai/evidences | 追加支撑证据 |
| /ai/incidents | 上报事故；同一 incident_key 的重复上报只形成一次处置 |
| /ai/incidents/pause | 暂停特定范围并设定复核时限 |
| /ai/incidents/tracking | 启动追踪 |
| /ai/incidents/review | 完成复核并圈定受影响患者与任务 |
| /ai/incidents/recover | 恢复运行（只解除暂停，不改写临床事实） |
| /ai/incidents/close | 关闭已恢复事故 |

查询接口（GET）：

- /ai/incidents/timeline?incident_id=…：事故处置与恢复时间线、受影响患者/任务；
- /ai/recommendations/trace?recommendation_id=…：建议、决定、任务与证据连线；
- /ai/versions/profile?version_id=…：版本的当前授权范围、未闭环风险与接管岗位；
- /ai/deployments/overdue：超过复核时限仍处暂停的部署。
