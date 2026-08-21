# Continuous Shadow Optimizer Implementation Plan

> 执行约束：影子优化不得阻塞正式分钟事件、不得发送 Webhook、不得写入正式订单；所有晋级均需满足完整前向样本门槛。

**目标：** 建立可长期运行的 Champion-Challenger 影子优化器，持续记录完整参数、因果回放候选策略，并在胜率优先门槛通过后安全晋级。

**架构：** 正式进程在完成闭合 1 分钟 K 线处理后，向有界跨进程队列投递不可变事件。独立影子进程维护当前 Champion 与最多 7 个 Challenger 的隔离状态，并写入独立的 `monitor.shadow.sqlite3`。第一阶段只自动优化和晋级已具备生产/回放同源实现的画像准入参数；完整运行参数与决策输入全部版本化保存，为后续参数族扩展提供可复现数据。

**技术栈：** Python、SQLite、`multiprocessing`、现有 `MonitorState`/`ProfileAdmissionPolicy`/`AccountSimulator`、`unittest`。

---

### Task 1：定义不可变事件、参数快照与生命周期规则

- 新增 `app/shadow_models.py`：市场事件、完整参数快照、实验组、评估结果与稳定哈希。
- 新增 `app/shadow_lifecycle.py`：胜率优先门槛、稳定排序、晋级冷却及回滚判定。
- 先写 `tests/test_shadow_models.py`、`tests/test_shadow_lifecycle.py`，覆盖哈希稳定性、300 单/7 完整日、方向胜率、订单量、连败和回撤门槛。

### Task 2：建立独立影子 SQLite 与 5 GiB 容量治理

- 新增 `app/shadow_storage_schema.py` 与 `app/shadow_storage.py`。
- 保存实验、参数快照、分钟游标、影子订单、每日汇总、评估、晋级/回滚历史与运行状态。
- 影子库独立执行 4/4.5/5 GiB 分级治理；普通决策明细压缩前先生成不可变日级分析汇总，保留未结订单和核心审计记录。
- 先写 `tests/test_shadow_storage_schema.py`、`tests/test_shadow_storage.py`。

### Task 3：接入非阻塞分钟事件扇出

- 在 `app/market_data.py` 正式处理成功后投递完整闭合分钟批次；队列满时只记录缺口，不等待、不重试。
- 固化事件 ID、数据源代次与 F&G 快照，禁止使用未来数据补写缺失分钟。
- 扩展 `tests/test_market_data.py`，证明正式更新先提交、影子发布后发生、发布失败不影响正式路径。

### Task 4：实现隔离的 Challenger 运行时

- 新增 `app/shadow_runtime.py`：每个参数组拥有独立订单、冷却、容量、画像、守卫和事件游标。
- 同一分析参数哈希共享候选计算；参数哈希不同则独立重算。
- 影子订单严格按到期分钟结算，缺少到期 K 线时保持未结，不用未来价格替代。
- 先写 `tests/test_shadow_runtime.py`，覆盖状态隔离、重启恢复、重复事件幂等、缺口冻结和同价平局判负。

### Task 5：实现后台进程与失效隔离

- 新增 `app/shadow_supervisor.py`：有界队列、子进程生命周期、健康状态、故障降级与无阻塞停止。
- 影子进程异常、数据库锁或容量告警均不得改变正式订单结果和延迟路径。
- 新增 `tests/test_shadow_supervisor.py`。

### Task 6：接入画像准入候选生成、晋级和回滚

- 基于 `app/profile_admission.py` 的规范参数与策略网格生成最多 7 个 Challenger。
- 每次实验固定完整参数快照和代码/数据源指纹；一次实验只改变一个参数族。
- 每日 07:50 评估，满足门槛后 08:00发起切换；正式应用、不可变回执持久化和ACK完成后才提交影子生命周期，7日内最多晋级一次，旧Champion保留14日用于回滚。
- 生命周期请求固化交易对、代次、实验、arm和参数哈希；拒绝、过期或回执失败不得改变Champion。
- 晋级只更新画像准入策略，不改评分、守卫、金额、方向容量等其他生产参数。
- 新增 `tests/test_shadow_profile_optimizer.py`。

### Task 7：服务端状态、只读 API 与页面摘要

- 在 `app/server.py` 启停影子主管，并暴露轻量状态和分页详情接口。
- 页面仅展示 Champion、Challenger 数、样本进度、最后事件、缺口、下次评估和容量状态。
- 扩展 `tests/test_server.py` 与静态页面测试，确保影子关闭时现有行为完全不变。

### Task 8：验证与文档收口

- 运行新增测试、相关状态/行情/存储/服务测试和完整测试集。
- 执行 `git diff --check`，检查影子关闭、队列溢出、子进程崩溃、重启恢复四条降级路径。
- 更新统一设计文档与唯一交接文档，记录已实现范围、默认关闭/开启参数和未自动晋级的后续参数族。

## 执行结果

八项任务均已在`feature/adaptive-resident-profiles`实现。额外审查加固了REST批次与正式路径一致性、同分析器原始计算共享、停机种子领先时实验替代、生命周期两阶段ACK、正式策略回执恢复、上下文代次校验、5GiB增量回收和30天决策日聚合。当前保持本地功能分支状态，不部署、不推送、不创建标签。
