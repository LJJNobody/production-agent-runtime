# Production Agent Runtime 实施路线图

最后更新：2026-08-21

## 使用方式

本文档是项目的长期实施入口。每次开发结束后需要同时更新：

- 阶段状态与任务复选框；
- 本轮完成内容和验证结果；
- 新增设计决策或范围调整；
- 尚未解决的风险。

状态约定：`[ ]` 未开始，`[~]` 进行中，`[x]` 已完成，`[-]` 暂不实施。

## 项目目标

将当前单进程原型演进为面向生产设计的 Durable AI Agent Runtime：基于 FSM、持久化
状态、分布式任务队列、幂等工具调用、Worker 故障恢复和全链路可观测性，支持
OpenAI-compatible 和 vLLM 推理服务。

在完成持久化、恢复和生产验证前，项目统一使用“面向生产设计”，不使用无法证明的
“生产级”表述。

## 目标架构

```text
Client
  -> FastAPI / Auth / Tenant Quota
  -> Admission Control + Idempotency
      -> PostgreSQL: Run / Step / Checkpoint / Outbox
      -> Redis Streams: Queue / Lease / Heartbeat
          -> Stateless Workers
              -> Agent FSM / DAG
              -> Tool Policy
              -> LLM Router
                  -> Mock / DeepSeek / OpenAI / vLLM
  -> OpenTelemetry -> Prometheus / Grafana / Traces / Logs
```

## 阶段 0：基线与计划

- [x] 保存实施路线图、验收标准和范围边界。
- [x] 记录初始测试基线：20 个测试通过，Ruff 通过，`pip check` 通过。
- [x] 确认当前目录不是 Git 仓库，暂不依赖 Git 保存开发进度。
- [x] 确认本机用于阶段 1～4，GPU 和云服务器延后租用。

## 阶段 1：有界调度与 API 工程基础

状态：本地实现与验证完成；远端 amd64/arm64 CI 等待接入 GitHub 仓库后执行。

目标：消除无界 `asyncio.Task`、无界 Run Registry 和重复提交风险，并让 API/CI 真正覆盖
运行链路。

- [x] 使用固定 Worker + 有界 `asyncio.Queue` 替换每次提交创建 Task。
- [x] 队列过载返回 429 和 `Retry-After`。
- [x] 增加 `Idempotency-Key`，相同载荷复用 Run，冲突载荷返回 409。
- [x] 增加终态 Run 的 TTL 与最大保留数量。
- [x] 统一 JSON 错误响应。
- [x] 启用 OpenAPI、Swagger 和 ReDoc。
- [x] 删除“必须恰好五个路由”的测试约束，替换为 API 契约/E2E 测试。
- [x] CI 安装 `api` 依赖并运行覆盖率、类型检查、API 测试。
- [~] CI 已配置 linux/amd64、linux/arm64 构建和健康检查；arm64 已在本机验证，
  amd64 等待仓库接入 GitHub Actions 后验证。

验收标准：

- 队列已满时新请求被明确拒绝，内存任务数量不会随请求无限增长。
- 相同幂等键和载荷只产生一个 Run；相同键不同载荷返回冲突。
- 超过 TTL 或最大保留数量的终态 Run 被回收。
- OpenAPI 包含提交、查询、取消、健康检查和错误响应定义。
- 本地测试、Ruff、类型检查、依赖检查、容器 Smoke Test 全部通过。

## 阶段 2：Durable Execution

目标：进程退出后状态不丢失，Worker 崩溃后任务可以安全接管。

- [x] 定义 `RunRepository`、`CheckpointStore`、`TaskQueue`、`LeaseManager`、
  `EventPublisher` 和 `SessionRepository` 接口。
- [x] 保留内存适配器用于快速单元测试，并增加 Repository、Checkpoint 单调性、
  有界 FIFO、Lease 过期接管、Event 深拷贝和 Session 容量契约测试。
- [x] `AgentRuntime` 通过 `TaskQueue` 和 `SessionRepository` 端口使用内存适配器，
  不再直接依赖 `asyncio.Queue` 和具体 Session 实现。
- [ ] 引入 PostgreSQL、Alembic 和 runs/run_steps/tool_executions/outbox_events 表。
- [ ] 使用状态版本号或行锁实现并发状态更新。
- [ ] 使用 Redis Streams Consumer Group 分发任务。
- [ ] 实现 Worker Lease、Heartbeat、超时接管和 Checkpoint 恢复。
- [ ] 实现 Transactional Outbox、事件发布重试和消费者去重。
- [ ] 明确采用 at-least-once 执行语义。
- [ ] 实现工具调用幂等键和结果复用。

验收标准：杀死执行中的 Worker 后，另一 Worker 从 Checkpoint 接管；重启所有容器后仍能
查询任务；已经完成的副作用工具不会重复执行。

## 阶段 3：可观测性与故障注入

- [ ] 接入 OpenTelemetry Trace，覆盖 API、排队、Agent Step、LLM、Tool 和 Checkpoint。
- [ ] 使用 Prometheus Histogram 记录排队、运行、LLM 和工具延迟。
- [ ] 增加结构化 JSON 日志、Trace ID 关联和敏感信息脱敏。
- [ ] 建立 Grafana Dashboard 和最小 SLO。
- [ ] 实现 Faulty LLM Proxy：429、Retry-After、5xx、长尾、挂起、断连、非法 JSON。
- [ ] 比较无保护基线与重试/限流/熔断启用后的 p50/p95/p99、成本和成功率。

验收标准：能够从一个 Run 定位完整 Trace；故障恢复行为和尾延迟可通过 Dashboard 与报告
复现，而不是仅依靠日志描述。

## 阶段 4：多租户与工具安全

- [ ] API Key/JWT 映射 tenant_id，所有数据访问强制租户过滤。
- [ ] 实现租户级 QPS、并发、队列、Token 和费用预算。
- [ ] 为工具增加 JSON Schema、权限等级、副作用标记和超时/并发策略。
- [ ] 增加工具 allowlist、网络出口 allowlist、文件路径限制和高风险操作确认。
- [ ] 建立 Prompt Injection、越权和审计脱敏测试。

验收标准：租户 A 无法读取或取消租户 B 的任务；未授权 Agent 无法执行高风险工具；密钥和
敏感 Prompt 不进入日志、指标或 Trace。

## 阶段 5：真实 vLLM 与性能报告

触发条件：阶段 2、3 的本地验收完成后，再按小时租用 24GB GPU 20～40 小时。

- [ ] 部署 7B/8B Instruct 模型和 vLLM OpenAI-compatible Server。
- [ ] 实现 Provider Router、fallback、SSE 流式输出和 Token-aware admission。
- [ ] 测量并发 1/4/8/16/32，短/中/长输入和多种输出长度。
- [ ] 报告吞吐量、p50/p95/p99、TTFT、TPOT、Tokens/s、GPU 显存和重试放大率。
- [ ] 保存机器规格、镜像 digest、模型版本、配置和原始 JSON 结果。

验收标准：所有性能结论都能由脚本和原始结果复现；mock 的 9.9x 不再作为生产性能证据。

## 阶段 6：云部署与 Kubernetes

触发条件：本地 Durable Execution、观测和安全主路径通过。

- [ ] 先在 8C16G x86_64 Ubuntu 云服务器完成 Docker Compose 部署。
- [ ] 再迁移到 K3s，配置 probes、requests/limits、PDB 和 RollingUpdate。
- [ ] 使用队列深度、等待时间或运行中任务数驱动 HPA，不只依赖 CPU。
- [ ] CI 构建、扫描镜像并执行部署后 Smoke Test。
- [ ] 仅开放 HTTPS；PostgreSQL、Redis 和观测后端不暴露公网。

验收标准：删除 Worker Pod 后任务恢复；API 滚动更新期间请求可用；负载变化能触发扩缩容。

## 阶段 7：面试交付物

- [ ] 完成 architecture、failure-semantics、durability、benchmark、threat-model、runbook 文档。
- [ ] 增加关键 ADR：FSM、at-least-once、PostgreSQL Outbox、Redis Streams。
- [ ] 准备 10 分钟故障恢复演示和可重复运行脚本。
- [ ] 使用真实测量结果重写简历项目描述和面试问答。

## 当前明确不做

- [-] 增加更多 Agent 模式。
- [-] 复杂前端和工作流画布。
- [-] 模型训练、微调和自研向量数据库。
- [-] A100/H100 多卡和三节点高可用 Kubernetes。
- [-] 宣称 exactly-once。
- [-] 在没有真实测试前写入固定成功率、加速比和 SLA。

## 实施日志

### 2026-08-21

- 建立路线图。
- 初始基线：20 tests passed；Ruff passed；pip check passed。
- 开始阶段 1。
- 阶段 1 核心实现完成：27 tests passed，分支覆盖率 80.68%，Ruff、Mypy、pip check 通过。
- arm64 runtime 0.2.0 镜像构建并启动成功：健康状态 healthy、非 root、只读根文件系统，
  镜像大小 51,582,714 bytes。
- 宿主机 HTTP 健康检查、OpenAPI 和并发幂等重放通过。
- 初始化本地 Git `main` 分支，建立根提交 `6e436c7`；远端 Actions 矩阵正在接入。
- 阶段 2 启动：完成六类基础设施端口及内存适配器，32 tests passed，覆盖率 81.38%。
- TaskQueue、SessionRepository 已接入运行时：33 tests passed，覆盖率 81.61%。
- GitHub CLI 2.97.0 已校验并完成账号 `LJJNobody` 的设备授权；创建私有远端和上传代码
  等待用户对具体目标仓库的显式确认。
