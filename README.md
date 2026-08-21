# Production-oriented Agent Runtime

一个面向生产设计的有限状态并发 AI Agent 运行时。它提供统一 LLM 抽象、Simple/ReAct/
Reflection/Plan-and-Solve 四种范式、有界异步调度、幂等提交、工具线程池、事件审计、
四层 API 容错和容器部署。

这是按照简历描述重建的可运行版本。代码不会把“成功率 99%+”“10 并发加速 9.9x”或
“镜像 150MB”写成固定结果；仓库提供复测工具，最终数字应以目标机器和真实供应商测量为准。

## 架构

```text
REST/CLI
   -> AgentRuntime（固定 Worker + 有界队列 + Run Registry + Session Store）
       -> 7-state FSM
       -> Simple | ReAct | Reflection | Plan-and-Solve
           -> ResilientLLMClient
              -> Token Bucket
              -> Circuit Breaker
              -> Retry + Exponential Backoff + Jitter
              -> OpenAI-compatible Provider（OpenAI/DeepSeek/本地服务）
           -> Tool Registry（每工具独立 Semaphore + Timeout + Thread Pool）
       -> EventBus（每订阅者独立有界队列/Worker）
           -> bounded AuditTrail
```

核心模块只依赖 Python 标准库。FastAPI、Pydantic 和 Uvicorn 作为 API 可选依赖，因此无
外部网络和 API Key 也可以先运行 mock 模式、测试 Agent 逻辑与并发状态。

## 目录

```text
src/agent_runtime/          核心运行时
  agents/                   四种 Agent 范式
  llm/                      统一接口、供应商与容错
  fsm.py                    7 状态 × 12 合法转换
  events.py                 发布订阅与审计
  tools.py                  工具注册、舱壁、超时和线程池
  runtime.py                并发调度、取消、重试和会话
benchmarks/                 并发加速与 API 成功率测量
config/                     mock 开发配置、生产配置模板
docs/                       状态机和测量口径
tests/                      核心单元与异步集成测试
Dockerfile                  多阶段、非 root 运行镜像
docker-compose.yml          单命令部署
.github/workflows/ci.yml    Python 3.9-3.12 矩阵与镜像构建
```

后续演进任务、阶段状态和验收标准统一维护在
[`docs/implementation-plan.md`](docs/implementation-plan.md)。

## 1. 本地运行

开发模式无需安装第三方依赖：

```bash
cd /path/to/production-agent-runtime
PYTHONPATH=src python -m agent_runtime.cli --config config/dev.json \
  run "分析令牌桶和熔断器的区别" --pattern simple --audit
```

分别验证四种范式：

```bash
PYTHONPATH=src python -m agent_runtime.cli --config config/dev.json \
  run "计算 6 * 7" --pattern react
PYTHONPATH=src python -m agent_runtime.cli --config config/dev.json \
  run "评审这个服务的容错设计" --pattern reflection
PYTHONPATH=src python -m agent_runtime.cli --config config/dev.json \
  run "制定一次 Agent 故障演练" --pattern plan_solve
```

mock 后端用于验证控制流，不代表真实模型回答质量。

安装 CLI 和 API：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[api,dev]"
agent-runtime --config config/dev.json serve --host 127.0.0.1 --port 8080
```

## 2. 统一 LLM 与四层防护

`LLMClient.complete()` 是唯一供应商接口。OpenAI 和 DeepSeek 都可通过兼容的
`/chat/completions` 地址接入，不依赖供应商 SDK；同步 HTTP 被投递到线程池，避免阻塞
asyncio 事件循环。

每一次逻辑请求依次经过：

1. 令牌桶限制瞬时突发和长期调用速率。
2. 熔断器在连续瞬时错误后进入 OPEN，恢复窗口后只放行有限 HALF_OPEN 探针。
3. 可重试错误执行指数退避；延时受最大值约束。
4. 每次退避加入随机抖动，降低大量请求同时重试的惊群风险。

HTTP 408、409、425、429 和常见 5xx/网络错误被视为瞬时错误；其他 4xx 和响应结构错误
立即失败。API Key 只从 `api_key_env` 指定的环境变量读取，不写入配置文件和审计日志。

生产配置：

```bash
cp config/production.example.json config/production.json
export AGENT_LLM_BASE_URL=https://api.deepseek.com/v1
export AGENT_LLM_MODEL=deepseek-chat
export AGENT_LLM_API_KEY=your-secret

agent-runtime --config config/production.json serve --host 0.0.0.0 --port 8080
```

也可以将地址替换为 OpenAI 或本地 OpenAI-compatible 服务。

## 3. 7 状态 × 12 条转换

状态为 `CREATED`、`READY`、`RUNNING`、`WAITING`、`SUCCEEDED`、`FAILED`、
`CANCELLED`。终态不允许继续转换；只有 `FAILED` 能通过显式 retry 回到 `READY`。

| 起始状态 | 合法目标状态 |
|---|---|
| CREATED | READY, CANCELLED |
| READY | RUNNING, CANCELLED |
| RUNNING | WAITING, SUCCEEDED, FAILED, CANCELLED |
| WAITING | RUNNING, FAILED, CANCELLED |
| FAILED | READY |

共 12 条白名单边。转换在每个 run 的异步锁内完成，状态修改、时间戳、转换记录和事件发布
属于同一临界区。非法转换抛出 `InvalidStateTransition`，不会修改 run。

## 4. 四种 Agent 范式

- Simple：携带有限多轮历史，单次生成回答。
- ReAct：模型只能返回结构化 JSON 决策；工具名必须来自 registry，工具输出以 observation
  回注，达到步数上限后强制生成最终答案。
- Reflection：draft、critique、revise 三阶段，最终只返回修订答案。
- Plan-and-Solve：先生成 JSON 计划，逐步执行，再统一综合答案。

所有 LLM 调用和工具调用都会使 FSM 从 `RUNNING` 进入 `WAITING`，返回后原子恢复；取消
操作可以与等待并发发生而不制造非法中间状态。

内置工具只有安全算术计算器和 echo。计算器使用 AST 白名单，不调用 `eval()`。业务工具
可通过 `ToolRegistry.register()` 注册；每个工具拥有独立并发上限与超时，单工具故障不会
占满其他工具的执行额度。

## 5. 事件总线和舱壁

事件先进入有界内存审计轨迹，再投递给订阅者。每个订阅者拥有自己的有界队列和 worker
数量；某个 handler 变慢、报错或积压，只影响该订阅，不会在发布路径传播异常。可通过
`runtime.audit_events(trace_id)` 查看一次 run 的完整状态与调用事件。

当前审计为内存实现，适合单实例演示。生产环境应订阅事件并写入 Kafka、数据库或日志平台；
不要把无限增长的完整 Prompt 直接写入审计系统。

## 6. REST API、幂等提交与多轮对话

API 启用了 OpenAPI、Swagger 和 ReDoc。核心业务端点为：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 存活和就绪状态 |
| GET | `/metrics` | Prometheus 文本指标 |
| POST | `/v1/runs` | 异步提交 Agent run |
| GET | `/v1/runs/{run_id}` | 查询状态、结果，可选审计 |
| POST | `/v1/runs/{run_id}/cancel` | 幂等式取消未结束 run |

提交：

```bash
curl -s http://127.0.0.1:8080/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-request-1' \
  -d '{"input":"解释熔断器的半开状态","pattern":"reflection","session_id":"demo-1"}'
```

相同幂等键与相同载荷会返回已有 Run；相同键但载荷不一致返回 409。运行时使用固定数量的
Worker 和有界等待队列，队列满时返回 429 与 `Retry-After`，不会为每个提交请求创建一个
长期等待的 `asyncio.Task`。

本地 API 文档：

```text
http://127.0.0.1:8080/docs
http://127.0.0.1:8080/redoc
http://127.0.0.1:8080/openapi.json
```

接口返回 `202` 和 run ID。轮询：

```bash
curl -s 'http://127.0.0.1:8080/v1/runs/RUN_ID?include_audit=true'
```

后续请求复用相同 `session_id` 即会携带最近若干轮历史。当前 registry 和 session 都在单进程
内存中，所以 Uvicorn 应保持单 worker；横向扩展需要把 run/session 存储迁移到 Redis 或
数据库，并引入任务队列。

## 7. Docker 与 Compose

Dockerfile 使用 builder/runtime 两阶段，只把 wheel 安装结果带入运行镜像；最终容器以
非 root 用户启动，并设置只读文件系统、drop capabilities 和 no-new-privileges。

```bash
cp .env.example .env
# 编辑 .env
docker compose up --build -d
docker compose ps
```

使用不访问外部模型的 mock 配置做容器 smoke test：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d
```

实际镜像大小必须在构建后测量：

```bash
python scripts/check_image_size.py production-agent-runtime:local --target-mb 150
```

基础镜像、CPU 架构和依赖版本都会影响结果；脚本在超过目标时返回非零状态，而不是仍然
宣称镜像为 150MB。

## 8. 复测 10 并发加速比

本地可控实验对同一 mock 延时工作负载分别使用并发 1 和并发 10：

```bash
python benchmarks/benchmark_concurrency.py \
  --tasks 10 --concurrency 10 --delay 0.2 \
  --output concurrency.benchmark.json
```

`speedup` 定义为 sequential wall time / concurrent wall time。异步调度开销会使它低于
理论上限 10；只有真实运行结果接近 9.9 时，简历才应保留该数字。真实 API 还要考虑限流、
网络、供应商批处理和尾延迟，不能用 mock 结果替代。

## 9. 复测 API 成功率

先用注入瞬时故障做回归测试：

```bash
python benchmarks/benchmark_reliability.py \
  --requests 1000 --concurrency 20 --attempts 4 \
  --simulate-failure-rate 0.30 --seed 42
```

simulation 只验证重试逻辑，不代表供应商 SLA。对真实兼容接口测量：

```bash
python benchmarks/benchmark_reliability.py \
  --requests 1000 --concurrency 20 --attempts 4 \
  --endpoint "$AGENT_LLM_BASE_URL" \
  --model "$AGENT_LLM_MODEL"
```

报告保留逻辑请求成功数、失败数、底层调用次数和重试次数。生产结论还应说明时间窗口、请求
类型、超时、供应商限额以及未加防护基线。详见
[`docs/benchmark-methodology.md`](docs/benchmark-methodology.md)。

## 10. 测试与 CI

```bash
python -m pytest --cov=agent_runtime --cov-report=term-missing --cov-fail-under=80
ruff check src tests benchmarks scripts
mypy src/agent_runtime
```

GitHub Actions 对 Python 3.9、3.10、3.11、3.12 做测试、覆盖率、Ruff 和 Mypy 检查，
并构建 linux/amd64、linux/arm64 镜像后启动容器做健康检查。

## 工程边界

- API 当前没有身份认证，公网部署前必须放在鉴权网关之后，并增加租户配额和输入上限。
- 内存 registry、session、audit 和 metrics 只适合单 worker；分布式部署需使用外部存储。
- ReAct 的工具调用来自模型生成，业务工具仍需做参数 schema、授权和资源隔离，不能直接
  暴露 shell、文件系统或数据库管理员能力。
- 进程内 Prometheus 指标在多 worker 下不会自动聚合。
- 当前有界队列、registry、session、audit 和 metrics 仍是单进程实现；阶段 2 将迁移到
  PostgreSQL、Redis Streams 和持久化 Outbox，进度以实施路线图为准。

## License

MIT
