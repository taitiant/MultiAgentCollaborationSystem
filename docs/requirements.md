# Multi-Agent Collaboration Runtime 需求说明（v1）

## 1. 目的与范围
- 构建领域无关的多智能体协作运行时，v1 先聚焦软件项目开发（需求澄清→设计→编码→测试→文档）。
- 支持用户创建/组合不同角色的智能体团队；允许人工在关键节点介入或审批。
- 架构需保持可演进：可插拔模型/工具（含 MCP）、RAG、知识图谱、并行调度等未来能力无需重构核心。

## 2. 术语
- Agent：实现 BaseAgent 接口的智能体。
- Capability：可匹配的最小能力单元（如 `code.edit`, `test.run`）。与 MCP 无关，但 MCP 工具可以提供某个能力。
- Task：调度的最小工作单元（领域无关）；用户可为一个团队/项目提交多个 Task。
- Team/Workgroup：一组协作的 Agent 配置。
- ModelAdapter：模型适配器，屏蔽具体 LLM/工具协议（含 MCP）。
- Plugin：事件钩子扩展（日志、指标、代码执行、RAG、KG 等）。
- Event / SystemState：事件溯源产生的状态；禁止直接修改状态，所有变更经事件。
- Artifact / Workspace：任务过程或结果文件；每个 Task 拥有独立工作目录。
- Snapshot：对运行时状态的周期/里程碑快照，用于快速恢复（不等同于代码快照）。
- StorageAdapter：抽象的工件存储接口，默认本地文件实现，可替换为 MinIO/S3。

## 3. 场景与非目标
- 场景：软件开发协作；可单 Agent 执行，也可多 Agent 分工（例如需求分析、架构、编码、测试）。
- 非目标：绑定特定业务流程；写死角色/模型；一次性覆盖所有工具生态。

## 4. 功能需求（MVP）
**4.1 Agent/Team 管理**
- API：register/update/remove Agent；字段含 role_name、capabilities、model_adapter、tools、metadata。
- Team 由 Agent 列表与协作策略组成，可通过配置加载。

**4.2 Task 生命周期**
- Task 字段：task_id、domain、required_capabilities、context、priority(0-100)、workspace_path（可选，未填则自动生成）。
- 状态：created → queued → running → waiting_user → completed/failed。
- 允许人工节点：产生 `await_user` 事件暂停，用户提交补充输入后 `resume`。

**4.3 调度**
- 实现：ManualScheduler，CapabilityMatchingScheduler。
- 语义：按优先级排序；在 Agent 轮换/空闲边界允许“限定抢占”，不打断正在执行的长操作。
- 输出：agent_id、rationale、planned_capabilities。

**4.4 消息与工件**
- AgentMessage 必须结构化：intent、capabilities_used、artifacts（类型、URI、hash、mime）、metadata（trace_id、cost、latency 等）。
- 禁止纯文本无结构输出。

**4.5 插件**
- 必选：LoggingPlugin（全量事件记录）。
- 可选：MetricsPlugin（基础调用/时延/错误计数）；CodeExecutionPlugin（沙箱运行命令/测试）。
- 插件通过注册表加载，失败不得阻断核心；错误写入事件流。

**4.6 模型适配**
- 至少实现 MockAdapter + 一个真实模型适配器；运行时可切换或按 Agent 绑定。
- 支持 MCP/工具调用作为适配层的一部分。

**4.7 存储**
- StorageAdapter 抽象：put/get/list/delete；默认 FileStore 将工件写入 `workspace/<task_id>/`。
- 事件与消息仅存路径/键，不直接嵌入大文件。

**4.8 观测性**
- 事件日志为事实来源；记录 trace_id/span_id；基础指标：模型调用次数、时延、错误率、任务状态转换计数。

**4.9 错误处理**
- 策略链：重试（可配置次数+退避）→ 降级模型/工具 → 人工仲裁 → 标记失败。
- 每步写入事件，保留决策链与原因。

## 5. 非功能与性能
- 单线程执行器但接口异步友好；为并行预留 executor 抽象。
- 上下文/工件尺寸限制可配置；防止内存膨胀。
- 状态可序列化；事件日志与快照带版本号与校验和。
- 安全：CodeExecutionPlugin 必须沙箱/白名单，最小权限；模型/工具凭证隔离存储。

## 6. 设计决策（已定）
- Capability 治理：集中注册 + 命名空间 + 版本后缀（例 `code.generate:v1`），启动做冲突检测。
- 优先级：按队列排序，阶段边界可插队；不打断执行中的步骤。
- 快照：事件为真相源，结合“周期 + 里程碑”快照；恢复失败时回退全量回放。
- 错误处理：按“重试→降级→人工→失败”。
- 工件存储：每 Task 独立目录（本地默认）+ StorageAdapter 抽象，未来可换 MinIO/S3，无需改核心。

## 7. 阶段规划
- 阶段 1（当前）：单线程；Manual + Capability 调度；Mock+单一真实模型；LoggingPlugin；CLI 驱动；本地 FileStore。
- 阶段 2：能力匹配评分；MetricsPlugin；周期快照；RAG/CodeExecution 接口准备。
- 阶段 3：多领域共存；模型路由策略；任务队列 + 线程池准备并行；知识图谱插件接口。
- 阶段 4：并行调度器；工具生态扩展（MCP/RAG/KG 协同）；Web 控制台；长程记忆。

## 8. 目录与配置约定（当前实现）
```
project-root/
  core/...
  domains/
    software_dev/
      agents/
      workflows/
      task_templates/
  plugins/
  adapters/
  workspace/           # 每个 task_id 一个子目录
  docs/
    requirements.md
    api.md
  config/
    capabilities.json
    workflow_templates/
      software_dev.json
  data/
    runtime.db
```

当前版本已从早期的静态 `runtime.json` / `agents.yaml` 配置，迁移到：

- `config/workflow_templates/software_dev.json`：软件开发参考流程
- `config/capabilities.json`：能力相关配置
- `data/runtime.db`：任务、事件、模型凭据、阶段绑定等运行时数据
- Web 管理页：通过 `/models.html` 完成模型凭据、模型与阶段绑定管理

示意：
```
{
  "workflow_template": "config/workflow_templates/software_dev.json",
  "capabilities": "config/capabilities.json",
  "runtime_db": "data/runtime.db",
  "workspace_root": "workspace/"
}
```

## 9. 开放问题（后续细化）
- Capability 版本弃用策略（兼容窗口多久、提示形式）。
- 人工节点的交互协议（CLI/HTTP/Webhook 细节）。
- 并行调度的冲突控制与资源限额策略。
