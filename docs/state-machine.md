# Agent 生命周期状态机

## 状态语义

- CREATED：run 对象已创建，但尚未进入调度队列。
- READY：可被 worker 获取，或由 FAILED 显式重试而来。
- RUNNING：Agent 控制逻辑正在本地执行。
- WAITING：正在等待 LLM、工具或外部 I/O。
- SUCCEEDED：已产生最终输出。
- FAILED：执行失败，保留错误和审计轨迹。
- CANCELLED：用户或关闭流程取消。

## 12 条合法边

```text
CREATED  -> READY
CREATED  -> CANCELLED
READY    -> RUNNING
READY    -> CANCELLED
RUNNING  -> WAITING
RUNNING  -> SUCCEEDED
RUNNING  -> FAILED
RUNNING  -> CANCELLED
WAITING  -> RUNNING
WAITING  -> FAILED
WAITING  -> CANCELLED
FAILED   -> READY
```

SUCCEEDED 和 CANCELLED 没有出边。FAILED 只有显式重试一条出边，避免失败任务被不透明地
自动重复执行。LLM 层自己的瞬时重试不会改变 run 的 FAILED/READY 状态，而是在同一次
WAITING 阶段内完成。

## 并发一致性

每个 run ID 有独立的 `asyncio.Lock`。条件转换（例如 WAITING 恢复 RUNNING）在锁内同时
检查当前状态和修改状态。若取消操作先把 WAITING 改成 CANCELLED，稍后返回的 LLM finally
块会发现预期状态不再成立并跳过恢复，因此不会出现 CANCELLED -> RUNNING。

转换同时写入源状态、目标状态、时间、原因，并向事件总线发布 `run.state_changed`。测试
固定校验状态数和白名单边数，新增状态或边必须显式修改测试和设计说明。
