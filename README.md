# MACS

一个面向软件开发场景的多智能体协作系统（Multi-Agent Collaboration System）。

MACS 的目标不是把任务硬塞进固定模板，而是让系统围绕一个任务，组织需求分析、方案设计、编码、测试、文档、评审与返工，并把全过程以可视化工作流和对话的方式呈现出来。

## 项目定位

MACS 当前聚焦于“软件开发任务编排”：

- 用户输入一个任务，例如“做一个小游戏”或“重构某个模块”
- 系统为任务建立工作区、上下文和事件流
- 不同阶段由不同智能体执行，并支持评审、返工和继续执行
- 前端以任务列表、画布、时间轴、对话、黑板等方式展示执行过程
- 所有产物和事件都落盘，便于追踪、续跑和复盘

这不是一个简单的“单轮生成代码”工具，而更接近一个可持续迭代的 AI 协作开发运行时。

## 当前能力

- 动态阶段实例执行：由 Leader 现场设计真实阶段，系统内部只保留轻量执行轮廓
- 阶段级评审与返工：支持评审失败后回到上一阶段继续修正
- 编码闭环优化：编码阶段可做基础冒烟校验，测试阶段可执行更完整验证
- 对话与协作记录：保留阶段对话、共享黑板、决策记忆
- 人工决策入口：当系统判断需要人工介入时，可在任务中补充意见
- AI 注册中心：支持凭据、模型、执行轮廓绑定、模型测试
- 多模型适配：支持 `openai-compatible`、`openai`、`codex`、`gemini`
- 任务可续跑：失败、返工、中断后可以继续推进
- 工作区隔离：每个任务有独立目录，保存设计、代码、测试和文档产物

## 系统界面

项目自带一个轻量前端，主要页面包括：

- `/`：主页与概览
- `/models.html`：模型/凭据/阶段绑定管理
- `/tasks.html`：任务列表
- `/task.html?task_id=...`：任务详情、工作流画布、日志、对话、黑板
- `/capabilities.html`：能力配置页
- `/skills.html`：技能策略页

## Skill / Capability 架构

MACS 现在采用分层增强机制：

- `Agent`：承担角色、记忆、对话与阶段目标
- `Skill`：增强智能体的方法论、约束和调用策略
- `Capability`：提供统一的可执行能力契约
- `Executor`：真正落到脚本、模型、HTTP API 或工作流

也就是：

```text
Agent -> (受 Skill 影响决策) -> 直接调用 Capability -> Executor
```

注意：

- `Skill` 不等于 `Capability`
- 智能体**可以直接调用 capability**
- 不要求“必须先通过 skill 才能调用 capability”
- `Skill` 负责“如何更聪明地做”
- `Capability` 负责“系统允许并且能够怎么做”

当前相关文件：

- `config/skills.json`
- `orchestration/skill_registry.py`
- `config/capabilities.json`
- `orchestration/capability_registry.py`
- `docs/skill_capability_architecture.md`

## 核心流程

一个典型任务会经历以下流程：

1. 创建任务
2. 生成任务计划与阶段实例
3. 按阶段实例执行
4. 评审
5. 失败返工 / 人工决策 / 继续推进
6. 产物落盘
7. 最终完成

默认参考流程位于 `config/workflow_templates/software_dev.json`，但项目当前的方向是：

- `Leader` 现场设计真实阶段实例
- 阶段名称、数量、顺序不固定
- 模板只作为参考 preset
- 系统内部仅保留少量 `execution_profile` 作为执行锚点

## 目录结构

```text
.
├── adapters/               # 各类模型适配器
├── config/                 # 工作流模板、能力配置等
├── core/                   # 核心数据结构
├── domains/software_dev/   # 软件开发域智能体
├── frontend/               # 静态前端页面
├── orchestration/          # 编排、协作、工作区清理、计划生成
├── plugins/                # 日志与指标插件
├── server/                 # FastAPI 服务入口
├── storage/                # 文件存储
├── tests/                  # 自动化测试
├── data/                   # SQLite 等运行时数据
└── workspace/              # 各任务独立工作区
```

几个关键文件：

- `server/app.py`：服务入口与主要 API
- `orchestration/graph_builder.py`：工作流编排核心
- `adapters/model_registry.py`：模型注册与路由
- `frontend/task.html`：任务详情页

## 技术栈

- 后端：FastAPI
- 编排：LangGraph
- 存储：SQLite
- 前端：原生 HTML / CSS / JavaScript
- 模型调用：OpenAI-compatible / Codex / Gemini 适配层
- 部署：Docker Compose

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
docker-compose up -d --build
```

启动后访问：

- `http://localhost/`：经由 Nginx 的前端入口
- `http://localhost:8000/`：FastAPI 静态页面入口

### 方式二：本地直接运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn server.app:app --reload --host 0.0.0.0 --port 8000
```

然后访问：

```text
http://localhost:8000/
```

## 模型配置

系统启动后，建议先到 `/models.html` 完成以下配置：

1. 添加凭据（Credential）
2. 在凭据下新增模型（Model）
3. 绑定各执行轮廓默认模型
4. 点击测试，确认模型可正常返回

当前支持的提供方类型：

- `openai-compatible`
- `openai`
- `codex`
- `gemini`

系统也支持通过环境变量注入密钥：

- `OPENAI_API_KEY`
- `GEMINI_API_KEY`
- `CODEX_API_KEY`

## 一次典型使用方式

1. 打开 `/models.html` 配好模型
2. 在主页或任务中心创建一个新任务
3. 打开任务详情页观察工作流推进
4. 如果阶段失败，可查看：
   - 事件流
   - 阶段对话
   - 黑板上下文
   - 工作区产物
5. 根据需要进行：
   - 续跑任务
   - 单独重跑某个阶段
   - 提交人工决策

## 任务产物与数据

每个任务会在 `workspace/<task_id>/` 下生成自己的工作区，常见内容包括：

- `analysis/`：需求分析
- `design/`：架构设计
- `code/`：代码产物
- `tests/`：测试结果
- `docs/`：README 等交付文档

运行时数据库位于：

- `data/runtime.db`

## API 概览

部分常用接口：

- `POST /tasks`：创建任务
- `POST /tasks/{task_id}/step`：执行 / 续跑任务
- `POST /tasks/{task_id}/stages/{stage_name}/rerun`：重跑指定阶段
- `POST /tasks/{task_id}/abort`：中止任务
- `GET /events`：查看事件流
- `GET /tasks/{task_id}/collaboration`：查看对话与黑板
- `POST /tasks/{task_id}/human-decisions`：提交人工决策
- `GET /models`：查看当前模型视图
- `POST /ai-registry/models/{model_id}/test`：测试某个模型
- `GET /capabilities`：查看能力目录与 binding 配置
- `POST /capabilities`：更新能力配置
- `GET /skills`：查看 skill 目录
- `POST /skills`：更新 skill 配置

## 测试

运行全部测试：

```bash
pytest -q
```

如果你使用 Docker：

```bash
docker-compose exec -T macs python -m pytest -q
```

## 当前状态

项目仍在持续迭代中，近期重点包括：

- 让 Leader/Manager 智能体按任务复杂度动态设计流程
- 加强智能体之间的多轮对话协作，而不是单向交接
- 改善前端画布、时间轴和失败反馈展示
- 强化编码/测试/返工闭环的一致性
- 提高任务续跑、状态恢复和证据对齐能力

如果你对多智能体软件开发编排、AI 协作式工程系统、任务可视化工作流感兴趣，这个项目会很适合继续一起打磨。

## 免责声明

这是一个实验性项目，适合研究、原型验证和内部工具探索。在生产环境使用前，建议补充：

- 权限控制
- 更严格的审计
- 更稳定的模型接入策略
- 更完整的测试与回滚机制

## License

本项目当前采用 `MIT` License，详见仓库根目录 `LICENSE`。
