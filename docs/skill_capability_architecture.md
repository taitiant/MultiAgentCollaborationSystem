# Skill / Capability 分层设计

## 目标

MACS 需要同时支持两类增强机制：

- `Skill`：增强智能体的方法论、约束、调用策略与角色经验
- `Capability`：提供统一的可执行能力契约，并最终落到脚本、模型、HTTP API 或工作流

这两者不能混为一谈。

## 推荐架构

```text
Leader
  -> 设计阶段实例 / 角色 / 闭环 / 人工决策点
  -> 为阶段实例分配 skills
  -> 为阶段实例分配 capabilities

Agent
  -> 受 skills 影响，决定如何思考、如何组织输出、何时调用能力
  -> 在授权范围内直接调用 capabilities

Capability
  -> 定义输入输出 schema、执行 binding、运行时审计契约

Executor
  -> internal tool / script / model / HTTP API / workflow API / MCP server
```

## 职责边界

### Skill

Skill 解决“会不会做、怎么做得更好”。

典型内容：

- 提示词工程
- 方法论
- Few-shot 风格
- 输出约束
- 对某类 capability 的使用建议

Skill 不直接代表执行权限。

### Capability

Capability 解决“能不能做、系统如何执行”。

典型内容：

- 统一 ID
- 输入输出 schema
- binding 类型
- 运行时默认参数
- 执行结果与审计信息

其中 `binding` 应尽量走配置驱动，而不是为每个外部工具单独写一套后端代码。

Capability 不负责高层策略。

## 关键原则

### 1. Agent 可以直接调用 capability

不要求必须通过 skill 作为中介。

原因：

- 基础能力不应该被多包一层
- Leader 可以动态组装轻量 agent
- Capability 是权限与执行接口，不应依赖额外包装才能工作

### 2. Skill 会影响 capability 的使用方式

Skill 可以声明：

- `preferred_capabilities`
- `required_capabilities`

但这不意味着 skill 是唯一入口。它只是帮助 agent 更合理地选择和使用 capability。

### 3. Leader 必须同时看 skills 与 capabilities

Leader 设计的是“可执行流程”，因此要考虑：

- 这个阶段该如何思考：看 `skills`
- 这个阶段能否真正执行：看 `capabilities`

例如：

- 没有 `asset.generate:v1`，就不应该设计依赖生图的落地流程
- 有 `asset.prompting:v1` skill 但没有素材生成 capability，只能做清单和提示词，不应假设能真正出图

## 当前落地方式

### Skill 注册表

位于：

- `config/skills.json`
- `orchestration/skill_registry.py`

目前支持：

- 默认 stage skill 分配
- planner 可见 skill 目录
- 运行时 skill guidance 注入

### Capability 注册表

位于：

- `config/capabilities.json`
- `orchestration/capability_registry.py`

目前支持：

- 默认 stage capability 分配
- binding 配置
- runtime invocation / auditing
- MCP server 绑定（配置服务器启动方式、工具链与返回契约）

### Leader 规划阶段

`orchestration/graph_builder.py` 中：

- 将 skill 目录和 capability 目录同时暴露给 leader
- 要求 leader 显式设计阶段实例的 `skills + capabilities`
- 允许阶段实例名称、顺序、数量动态变化
- 系统内部仅保留 `execution_profile` 作为执行锚点
- 明确说明：
  - skill 不等于 capability
  - agent 可以直接调用 capability
  - 若 skill 偏好某些 capability，应同步分配

### 运行时阶段执行

构图执行时：

- stage 结构中保留 `skills`
- runtime collaboration context 会注入：
  - 黑板 / 长期记忆 / 前置文档
  - skill guidance
  - capability invoke guidance

因此 agent 在执行时能同时得到：

- 如何思考
- 能调用什么

## 为什么不是直接照搬 OpenClaw 风格 Skill

OpenClaw 风格 skill 更像“可安装的技能包/提示词插件”。

MACS 当前更需要的是：

- 多智能体工作流内的显式执行编排
- 阶段级能力审计
- 与前端画布、时间轴、返工链路联动

因此现阶段采用：

- Skill 负责认知增强
- Capability 负责执行底座

未来如有需要，可以在这之上再增加：

- Skill package 安装机制
- marketplace / workspace-local skill
- 更细粒度的 agent skill 挂载 UI

## 后续建议

下一步可继续补：

1. `skills.html` 管理页
2. Leader 流程审计中的 skill / capability 配对检查
3. agent 级 skill 挂载，而不仅是 stage 级
4. 支持 skill 包引用本地模板、脚本和检查器

## MCP 接入原则

为避免“每接一个 MCP 就写一次代码”，推荐把大多数新 MCP 都接成配置：

- `binding_type = mcp_server`
- 在 `config/mcp_servers.json` 或 `/mcp-servers` API 中登记全局 `mcpServers`
- 在 binding 中填写：
  - `mcp.server_name`
  - `mcp.transport`
  - `mcp.tools`（工具链与参数模板）
- 若 `mcp.server_name` 已存在于全局注册表中，系统会自动补全：
  - `command / args / env / cwd`
  - `url / headers`
- 若该 capability 没有专用 handler，但配置了 MCP binding，运行时也会生成通用 `capability_request` 请求清单，而不是直接跳过

全局注册表示例：

```json
{
  "mcpServers": {
    "docx-mcp": {
      "command": "uvx",
      "args": ["docx-mcp"]
    },
    "ace-tool": {
      "command": "npx",
      "args": ["ace-tool", "--base-url", "YOUR_BASE_URL", "--token", "YOUR_TOKEN"]
    }
  }
}
```

这意味着后续新增大多数 MCP 服务时，优先做法应是：

1. 在前端新增一个 capability 或复用已有 capability
2. 选择 `MCP 服务` 执行方式
3. 填写服务器命令和工具链
4. 在阶段上通过 `capability_options` 传入实际参数

只有当某个能力需要复杂的本地结果落盘、强耦合状态管理或特殊审计逻辑时，才值得补专用 Python handler。
