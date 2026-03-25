# Multi-Agent Collaboration Runtime API 合同（v1）

## 1. 调用方式
- 暴露统一 Application API，可由 CLI/HTTP/Web UI 调用；此文档描述逻辑接口与数据契约。

## 2. 数据结构
### 2.1 AgentConfig
```
{
  "id": "patcher",
  "role_name": "PatchAgent",
  "domain": "software",
  "capabilities": ["code.edit:v1", "code.diff:v1"],
  "model_adapter": "gpt4o",
  "tools": ["shell", "git"],
  "metadata": {"owner": "team-a"}
}
```

### 2.2 Capability 命名规范
- 采用命名空间 + 语义名 + 版本：`<namespace>.<action>:v<major>`，如 `code.generate:v1`。
- 集中注册，启动时校验冲突与弃用提示。

### 2.3 Task
```
{
  "task_id": "task-001",
  "domain": "software",
  "required_capabilities": ["code.edit:v1", "test.run:v1"],
  "context": {"repo": "git://...", "spec": "Implement login"},
  "priority": 80,
  "workspace_path": "workspace/task-001"   // 可选，未填则自动创建
}
```

### 2.4 AgentMessage
```
{
  "message_id": "msg-123",
  "task_id": "task-001",
  "actor_id": "patcher",
  "domain": "software",
  "intent": "apply_patch",
  "capabilities_used": ["code.edit:v1"],
  "artifacts": [
    {"type": "diff", "uri": "workspace/task-001/diff.patch", "hash": "sha256:...", "mime": "text/x-diff"}
  ],
  "metadata": {"trace_id": "...", "latency_ms": 1200, "cost": 0.003}
}
```

### 2.5 Event
```
{
  "event_id": "evt-789",
  "timestamp": "2026-02-26T10:00:00Z",
  "actor_id": "scheduler",
  "task_id": "task-001",
  "event_type": "AgentSelected",    // 典型：TaskCreated, AgentSelected, MessageProduced, TaskCompleted, TaskFailed, AwaitUser, Resume
  "payload": {"agent_id": "patcher", "reason": "capability_match"}
}
```

### 2.6 Snapshot 元数据
```
{
  "snapshot_id": "snap-42",
  "version": "v1",
  "event_offset": 1024,
  "hash": "sha256:...",
  "created_at": "..."
}
```

### 2.7 StorageAdapter 接口（抽象）
- put(task_id, rel_path, bytes|stream) -> uri
- get(uri) -> bytes|stream
- list(task_id, prefix?) -> [uri]
- delete(uri)
默认实现：FileStore，根目录 `workspace/`。

### 2.8 CodeExecutionPlugin 接口（建议）
- run(command: str, cwd: str, env: dict, timeout_s: int) -> {stdout, stderr, exit_code, duration_ms}
- 必须沙箱/白名单；失败事件写入日志。

## 3. 核心 API
返回值均为 JSON，对错误使用统一错误模型：`{code, message, details}`。

- register_agent(agent: AgentConfig) -> {status}
- remove_agent(agent_id: str) -> {status}
- list_agents() -> [AgentConfig]
- submit_task(task: Task) -> {task_id}
- pause_task(task_id: str, reason?: str) -> {status}
- resume_task(task_id: str, user_input?: dict) -> {status}
- set_scheduler(name: str, config?: dict) -> {status}
- bind_model(agent_id: str, model_adapter: str) -> {status}
- step() -> {status, actions?: [AgentMessage]}   // 驱动一次调度与执行
- get_state(task_id?: str) -> {state, updated_at}
- get_event_log(task_id?: str, offset?: int, limit?: int) -> [Event]

## 4. 调度器契约
- 输入：Task + SystemState（含队列、Agent 列表、历史记录）。
- 输出：`{agent_id, rationale, planned_capabilities}`；若无合适 Agent，可返回 null 触发 `AwaitUser` 或排队。
- 必须无副作用；禁止直接改状态。

## 5. 人工节点协议
- 触发：Agent 或调度器产生 `AwaitUser` 事件，附带需要的输入说明。
- 用户通过 `resume_task(task_id, user_input)` 提供内容；系统生成 `Resume` 事件并继续调度。

## 6. 工作区与工件
- 默认每 Task 一个目录：`workspace/<task_id>/`，子内容按 Agent/阶段分类（建议 `artifacts/`, `logs/`, `patches/`, `reports/`）。
- 事件与消息引用工件的 URI；不直接内嵌大文件。

## 7. 状态快照
- 触发策略（可配置）：每 N 条事件或每 T 分钟，及里程碑事件（TaskCompleted/Failed）。
- 内容：队列、任务状态、调度器内部元数据、最近事件偏移；不必包含文件工件。

## 8. 错误处理（执行层面）
- 对模型/工具调用：
  1) 重试（配置次数/退避）；
  2) 降级（备用模型/工具）；
  3) `AwaitUser`（可选）；
  4) 失败并生成 `TaskFailed` 或 `MessageFailed` 事件。
- 所有步骤写入事件，便于审计与重放。

## 9. 安全与权限
- CodeExecutionPlugin 默认禁用敏感命令；需显式白名单或沙箱。
- 模型/工具凭证通过安全配置注入，不写入事件/日志。

## 10. 兼容与演进
- 新能力必须注册命名空间与版本；弃用时保留兼容窗口并输出警告事件。
- 新调度器/插件需遵守同样输入输出契约即可热插拔；核心不感知领域名或模型细节。
