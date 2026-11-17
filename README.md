# CycleFlow 机器人样机迭代数据服务

CycleFlow 面向样机研发团队，接收设计修订、部件批次、生产工单和质量试验等独立来源的事件。SQLite 同时保存原始事件身份和派生的业务状态。报表将设计版本、工单、工序耗时、物料批次、试验结果和质量问题连接起来，供团队解释每次迭代的交付效率。

## 运行

依赖版本锁定在 `requirements.txt`。在项目目录运行 `python -m unittest discover -s tests -v` 执行业务测试，`python -m compileall -q cycleflow` 检查源码，`python -m cycleflow.demo` 运行合成数据验收。安装所列依赖后，可设置 `CYCLEFLOW_DB` 为可写的数据库路径，再运行 `uvicorn cycleflow.api:app` 启动 HTTP 服务。命令行帮助为 `python -m cycleflow --db cycleflow.sqlite3 --help`。

演示仅使用临时数据库和本地合成产品。它分两次导入同一清单，验证试验良率，冻结月报，并审计存储的状态链；正常运行无需访问外部服务。

## 事件协议

向 `/events` 提交 `source`、`external_id`、`kind`、带时区的 `occurred_at` 和对象类型的 `payload`。相同事件重放返回既有编号；相同身份提交不同内容返回冲突。缺少前置对象或暂时缺料的事件进入待处理状态，可调用 `/events/{id}/retry` 补齐后重试。其他业务无效事件仍保留拒绝原因。批量接口每次使用稳定的批次编号和完整事件数组；系统核对清单指纹，从已提交的行继续。

事件类型包括 `design.release`、`design.retire`、`lot.receive`、`lot.quarantine`、`job.open`、`job.stage_start`、`job.stage_finish`、`job.scrap`、`plan.create`、`test.record`、`equipment.register`、`calibration.record`、`calibration.invalidate` 和 `calibration.reinstate`。设计包含产品、版本、部件数量及可选的父版本。部件批次包含料号、供应商和正整数数量。工单绑定一个设计和产量，成功预留物料后依次经过加工、装配和集成。试验计划绑定精确的产品版本，受测数量必须覆盖该工单全部单位。

## 设备校准与试验有效性

每条 `test.record` 必须携带 `device_id`。设备通过 `equipment.register` 登记，`calibration.record` 记录半开有效期区间 `[valid_from, valid_to)`；同一设备可以存在相互重叠的证书，任一仍生效的区间覆盖试验时间即视为有效。试验是否可验证完全按事件实际时间（`occurred_at`）判定，与事件到达顺序无关：迟到登记的证书、迟到的作废都会触发全量重算。

`calibration.invalidate` 撤销一张证书（覆盖区间内的历史试验随之失去校准依据），`calibration.reinstate` 用于撤销错误告警，恢复原区间的覆盖关系。作废后没有有效校准覆盖的试验不会被当作失败，也不会被删除：其结论标记为未验证（`verified=false` 与 `verify_reason`），工单进入 `recheck` 状态并从良率分子分母中整体剔除，汇总以 `unverified_units` 单独列示。据此开出的返工工单通过 `rework_premise_unverified` 质量问题和 `/quality/reviews` 的 `rework_impacted` 列表标出。

补做试验同样以 `test.record` 提交，仅当工单处于 `recheck` 且新试验时间晚于原试验时接受；原试验保留并标记 `superseded=1`（计入 `superseded_units`），新试验获得校准验证后工单自动恢复为 `completed` 或 `failed`，良率、周度、队列和供应商视图回到一致口径。`/quality/reviews` 汇总待复核结论与受影响返工，`/equipment/{device_id}` 展示设备的证书链。

## 报表与发布

请求 `/reports/{product}?window_start=...&window_end=...` 获取当前视图。时间窗含起点、不含终点，以工单创建时间决定归属。报表提供按版本汇总、良率、工序时长、等待时长、来源事件编号和质量问题。周度、返工分组、瓶颈和供应商视图使用相同的已接受事实。供应商试验归因可以重叠，不能跨供应商直接求和。

向 `/releases` 提交发布编号、产品、时间窗和预览返回的 `expected_fingerprint`。未完成工单或窗口内未解决事件阻止发布；处于 `recheck` 的未验证结论同样阻止发布。发布后保留原报告和来源事件头；`/releases/{id}/freshness` 比较当前视图与冻结视图，不改写历史，并通过 `change_reasons` 逐条说明现行报告的变化来源（工单状态迁移、试验验证状态变化、补做试验取代、汇总字段变动），使历史发布保持原样而现行口径仍可解释。`/quality/audit` 检查事件身份、设计版本链、物料预留、工序顺序、试验记录、校准覆盖与发布摘要。
