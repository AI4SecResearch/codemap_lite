# ICSLpreprocess — 架构设计文档

## Context

目标：构建名为 ICSLpreprocess 的代码预处理工具，将目标代码目录解析为完整的函数级调用图（Call Graph），存储于 Neo4j，并提供前端界面供人工审阅修复结果。

**核心职责**：调用图构建 + 间接调用修复 + 修复结果审阅。不做污点传播审计。

**工作流**：
1. codewiki_lite 先行运行，产出 source 点（REST API 提供）
2. ICSLpreprocess 解析代码 → 构建调用图 → 从 source 点出发修复间接调用 → 存入 Neo4j
3. 前端浏览调用图 + 审阅 LLM 修复的边 + 人工增删改

**整合的现有代码仓**：
- **AI4CParser** (`C:\Task\AI4CParser-master`) — tree-sitter 解析 + 调用图构建 + 间接调用解析
- **codewiki_lite** (`C:\Task\codewiki_lite`) — source 点检测（通过 REST API 消费）
- **codemap** (`C:\Task\codemap`) — Neo4j 存储层

---

## 1. 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│              Layer 5: Frontend (React + Cytoscape.js)            │
│   CallGraph Viewer │ Node Inspector │ Review Panel │ Progress    │
├─────────────────────────────────────────────────────────────────┤
│              Layer 4: REST API (FastAPI)                         │
│   /graph  /calls  /source-points  /review  /analyze             │
├─────────────────────────────────────────────────────────────────┤
│              Layer 3: Repair Agent Layer                         │
│   RepairOrchestrator │ CLI Agent (subprocess) │ FeedbackStore    │
│   GateChecker │ icsl_tools.py │ ProgressTracker (内存)           │
├─────────────────────────────────────────────────────────────────┤
│              Layer 2: Graph Storage (Neo4j 5.x)                  │
│   Neo4jStore │ IncrementalUpdater │ QueryEngine                  │
├─────────────────────────────────────────────────────────────────┤
│              Layer 1: Static Analysis                            │
│   CallGraphBuilder │ SymbolExtractor │ StaticResolver (3-layer)  │
├─────────────────────────────────────────────────────────────────┤
│              Layer 0: Parsing & Ingestion                        │
│   PluginRegistry │ CppPlugin │ FileScanner │ FileCache (SHA256)  │
└─────────────────────────────────────────────────────────────────┘

外部依赖：codewiki_lite REST API (source 点)
```

---

## 2. 两阶段解析策略

### 阶段 1：静态解析（全量解析，快速、确定性高）
- **全量解析所有文件**（tree-sitter 解析速度快，不按需裁剪）
- 使用 AI4CParser 的前 3 层解析器：
  1. **签名匹配** — 函数指针类型约束过滤候选
  2. **数据流追踪** — 追踪变量赋值来源
  3. **上下文推断** — 变量名/函数名模式匹配
- 输出：尽力而为的调用图（直接调用 100% 解析，部分间接调用解析）
- 未解析的间接调用标记为 GAP

### 阶段 2：修复 Agent（每个 source 点一个 CLI Agent）
- 从 codewiki_lite API 获取 source 点列表
- 为每个 source 点启动一个 CLI subprocess Agent（并发池控制）
- Agent 自主 BFS 遍历可达路径，修复所有 UnresolvedCall
- 只修复 source 可达路径上的 GAP（非可达的不修复）
- Agent 完成后执行门禁验证，未通过则重启（最多 3 次/GAP）

---

## 3. 修复 Agent 设计

### 执行模型

每个 **source 点**启动一次独立的 **CLI subprocess 调用**（opencode 或 claudecode CLI）。Agent 自主完成该 source 点下所有 GAP 的修复。

```
Orchestrator (Python)
    │
    ├─ [并发池: max 5，source 间并发，source 内串行]
    │
    ├─► Source A → subprocess: claudecode -p "修复 source A 的 prompt"
    │                 │ Agent 自主 BFS：query-reachable → 修复 GAP → 再查询 → ... → 无新 GAP
    │                 ↓
    │              门禁：check-complete → 通过 ✅ / 未通过 → 重启（最多 3 次/GAP）
    │
    ├─► Source B → subprocess: claudecode -p "修复 source B 的 prompt"
    │                 ...
    │
    └─► Source C → subprocess: claudecode -p "修复 source C 的 prompt"
                      ...
```

**关键设计决策**：
- **Agent 粒度**：每个 source 点一次 CLI 调用（Agent 自主 BFS 遍历 + 修复所有可达 GAP）
- **Agent 形态**：Subprocess CLI（opencode/claudecode），非 SDK API 调用
- **工作目录**：目标代码目录（Agent 可直接读文件、搜索代码）
- **图状态获取**：Agent 通过 `icsl_tools.py` 自主查询 Neo4j
- **Neo4j 写入**：Agent 调用 `icsl_tools.py write-edge`
- **并发控制**：固定并发池（asyncio.Semaphore，默认 max=5，source 间并发，source 内串行）
- **超时**：不限时，Agent 自然完成
- **冲突处理**：Agent 修复前检查边是否已存在，已存在则跳过

### 门禁机制

每个 source 点的 Agent 完成后，Orchestrator 执行门禁验证：

```
Agent 退出
    ↓
Orchestrator: icsl_tools.py check-complete --source <id>
    ↓
查询该 source 可达路径上是否还有 status="pending" 的 UnresolvedCall
    ↓
无残留 → SourcePoint.status = "complete" ✅
有残留 → 残留 GAP 的 retry_count++
    ├─ retry_count < 3 → 重启 Agent（新 CLI subprocess）
    └─ retry_count ≥ 3 → GAP.status = "unresolvable"
                          SourcePoint.status = "partial_complete"
```

**GAP 级别重试**：每个 UnresolvedCall 独立追踪 retry_count，最多 3 次。已修复的 GAP 不会被重复处理（Agent 检查边已存在后跳过）。

### Agent 内循环

Agent 的工作流程（CLAUDE.md 中描述）：

```
1. icsl_tools.py query-reachable --source <id>
   → 获取可达子图 + UnresolvedCall 列表
2. 遍历每个 UnresolvedCall：
   a. 读源码（利用 CLI 内置的文件读取能力）
   b. 分析调用上下文（结合候选列表作为参考）
   c. 确定调用目标
   d. icsl_tools.py write-edge → 写入 CALLS 边 + 创建 RepairLog + 删除 UnresolvedCall
3. 再次 query-reachable → 发现新的 UnresolvedCall（修复后新可达的）
   - 有 → 回到步骤 2
   - 无 → 退出（交给 Orchestrator 门禁）
```

### 终止条件（Agent 内部）
- 到达系统库/标准库函数（无源码）
- 搜索整个代码库找不到函数实现
- 调用链形成环（递归检测）
- 所有可达 UnresolvedCall 已处理
- 到达 sink 点后**继续追踪**（发现所有可达路径）

### Agent 上下文注入

利用 CLI 的 CLAUDE.md 机制，Orchestrator 在目标代码目录临时生成：

```
目标代码目录/
├── CLAUDE.md              # Agent 角色定义 + 工具使用说明 + 反例库引用
├── .claude/
│   └── settings.json      # Hook 配置
└── .icslpreprocess/
    ├── icsl_tools.py       # 图查询 + 边写入 + 门禁 CLI 工具
    ├── counter_examples.md # 反例库（泛化后全量注入）
    ├── config.yaml         # Neo4j 连接配置（icsl_tools.py 读取）
    ├── hooks/
    │   ├── log_tool_use.py
    │   └── log_notification.py
    └── logs/               # Agent 推理日志
        └── repair/{source_id}/
```

**Orchestrator 在启动每个 Agent 前**：
1. 生成 CLAUDE.md（角色 + 任务 + 工具说明 + 反例引用 + 终止条件）
2. 复制 icsl_tools.py 到 .icslpreprocess/
3. 生成 config.yaml（Neo4j 连接信息）
4. 更新 counter_examples.md（最新反例库）
5. 配置 .claude/settings.json（hooks）
6. **Agent 完成后清理所有注入文件**（仅清理 Orchestrator 生成的文件，如果目标目录已有 CLAUDE.md 则备份后恢复）

### 推理过程捕获（Hook 机制）

通过 CLI 的 Hook 机制提取 Agent 推理信息：

```jsonc
// .claude/settings.json
{
  "hooks": {
    "PostToolUse": [{
      "command": "python .icslpreprocess/hooks/log_tool_use.py"
    }],
    "Notification": [{
      "command": "python .icslpreprocess/hooks/log_notification.py"
    }]
  }
}
```

Hook 脚本将推理信息写入：
1. **推理日志**：`logs/repair/{source_id}/{gap_id}.jsonl`（JSONL 格式，每次 PostToolUse 追加一行：工具名、参数、结果摘要、时间戳）
2. **进度文件**：`logs/repair/{source_id}/progress.json`（当前 source 已修复 GAP 数、总 GAP 数、当前处理的 GAP ID）
3. **Neo4j RepairLog**：修复完成后，从 `.jsonl` 提取关键步骤写入 RepairLog 节点的 reasoning_summary

**进度通信机制**：
- Hook 脚本写入 progress.json（Agent subprocess 内）
- Orchestrator 定期读取 progress.json，更新内存字典
- 前端轮询 Orchestrator 的 `/api/v1/analyze/status` 获取进度

### LLM 后端配置
```yaml
# config.yaml (agent 部分)
agent:
  backend: "claudecode"  # "claudecode" | "opencode"
  max_concurrency: 5
  retry_failed_gaps: true        # 跨运行重试：下次运行时重置 unresolvable GAP 的 retry_count
  claudecode:
    command: "claude"
    args: ["-p", "--output-format", "text"]
  opencode:
    command: "opencode"
    args: ["-p"]
```

### 反馈机制（反例库）

**存储格式**：Markdown 文件（`.icslpreprocess/counter_examples.md`），通过 CLAUDE.md 引用，Agent 启动时自动加载。

**反例生成流程**：
1. 审阅标记边为错误 → 提取反例（调用上下文 + 错误目标 + 正确目标）
2. **泛化**反例（去掉具体变量名/行号，保留模式特征）
3. 用 **LLM 判断相似性**：检查反例库中是否已有类似反例
4. 相似 → **总结合并**为更通用的反例
5. 不相似 → 新增到反例库
6. 更新 `counter_examples.md` 文件

**注入方式**：泛化去重后，全量注入 prompt（通过 CLAUDE.md 引用）

---

## 4. Neo4j Schema

### 节点类型

| 节点类型 | 用途 | 关键属性 |
|---------|------|---------|
| `File` | 源文件 | file_path, hash, primary_language |
| `Function` | 函数 | signature, name, file_path, start_line, end_line, body_hash |
| `SourcePoint` | 外部输入入口 | entry_point_kind, reason, function_id, status |
| `UnresolvedCall` | 未解析的间接调用 | caller_id, call_expression, call_file, call_line, call_type, source_code_snippet, var_name, var_type, candidates, retry_count, status |
| `RepairLog` | 修复审计日志 | caller_id, callee_id, call_location, repair_method, llm_response, timestamp, reasoning_summary |

### 关系类型

| 关系 | 方向 | 属性 |
|------|------|------|
| `DEFINES` | File → Function | — |
| `CALLS` | Function → Function | resolved_by, location, call_type |
| `HAS_GAP` | Function → UnresolvedCall | — |
| `IS_SOURCE` | Function → SourcePoint | — |

注：RepairLog 不通过关系关联，而是通过属性引用 CALLS 边（存储 caller_id + callee_id + location 三元组唯一定位）。

### CALLS 边属性
```
{
  resolved_by: "symbol_table" | "signature" | "dataflow" | "context" | "llm",
  call_type: "direct" | "indirect" | "virtual",
  location: {file: str, line: int}
}
```

注：不再需要 `valid` 字段。审阅标记错误时直接删除边并重建 UnresolvedCall，而非保留 invalid 状态。

### 索引
```cypher
CREATE INDEX idx_file_hash FOR (n:File) ON (n.hash);
CREATE INDEX idx_function_file FOR (n:Function) ON (n.file_path);
CREATE INDEX idx_function_sig FOR (n:Function) ON (n.signature);
CREATE INDEX idx_source_kind FOR (n:SourcePoint) ON (n.entry_point_kind);
CREATE INDEX idx_calls_resolved FOR ()-[r:CALLS]-() ON (r.resolved_by);
CREATE INDEX idx_gap_status FOR (n:UnresolvedCall) ON (n.status);
CREATE INDEX idx_gap_caller FOR (n:UnresolvedCall) ON (n.caller_id);
```

### UnresolvedCall 生命周期

```
status: "pending" → Agent 修复成功 → 删除节点（审计信息转入 RepairLog）
                 → 3 次重试后仍失败 → status = "unresolvable"
                 → 下次运行时 → 重置 retry_count=0, status = "pending"（重新尝试）
```

修复成功时：创建 CALLS 边 + RepairLog 节点 + 删除 UnresolvedCall 节点。
前端通过 RepairLog 的 `caller_id + callee_id + call_location` 定位对应的 CALLS 边，展示修复过程。

### SourcePoint 状态

```
status: "pending" → "running" → "complete"（所有可达 GAP 已修复）
                             → "partial_complete"（部分 GAP 标记为 unresolvable）
```

---

## 5. 前端设计

### 两种浏览模式
1. **从 source 出发**：source 点列表 → 选择 → 展开可达调用链 → 审阅修复边
2. **从函数浏览**：文件树 → 函数列表 → 选择 → 查看 callers/callees → 审阅

### 审阅交互
- 审阅对象：单条 CALLS 边（特别是 resolved_by="llm" 的）
- 操作：标记正确 / 标记错误 / 手动添加边 / 手动删除边
- **标记错误时**：
  1. 可填写正确目标 → 触发反例生成（异步泛化）
  2. 立即删除该 CALLS 边 + 对应 RepairLog
  3. 重新生成 UnresolvedCall 节点（retry_count=0）
  4. 触发 Agent 重新修复该 source 点（异步）

### 进度感知
- Agent 每次写入 Neo4j 时打点记录进度
- 前端轮询 `GET /api/v1/analyze/status` 获取进度

### 可视化策略
- **全量渲染** + **分层聚合**
- 利用 codewiki_lite 返回的模块信息进行层次化展示
- 顶层：模块级聚合视图（模块间调用关系）
- 展开：模块内函数级调用图
- Cytoscape.js 支持 compound nodes（父子嵌套）实现分层

### 页面结构
```
frontend/src/pages/
├── Dashboard.tsx          # 概览：source 点数量、已修复/未修复 GAP 数、待审阅边数
├── SourcePointList.tsx    # Source 点列表，按 kind 分组
├── FunctionBrowser.tsx    # 文件树 + 函数列表
├── CallGraphView.tsx      # 调用图可视化（Cytoscape.js）
├── ReviewQueue.tsx        # 待审阅边列表（resolved_by=llm 且未审阅的）
└── FeedbackLog.tsx        # 反例库浏览
```

### 跨页面 drill-down 契约
- Dashboard 的 StatCard 支持可选 `to` 链接，点击跳转到对应子视图的**预筛选**状态。
- `Unresolved GAPs` → `/review?status=pending`；`Unresolvable` → `/review?status=unresolvable`。
- 非 backlog 类 StatCard 也承载导航意图：`Source Points` → `/sources`、`Files` → `/functions`（FunctionBrowser 左栏即文件树）、`Functions` → `/functions`。目标子视图无需预筛选时，`to` 直接指向列表根路径；`to` 约定是轻量 affordance（右侧 `›` 暗示可点），不强制所有 StatCard 都带。
- `ReviewQueue` 挂载时读 `?status=` query param，若值 ∈ `{all, pending, unresolvable}` 就作为初始 `statusFilter`；之后用户手动切换筛选也双向同步到 URL，保证链接可分享/可书签。
- ReviewQueue 的 "反例已保存" 横幅把 pattern 链到 `/feedback?pattern=<encoded>`；`FeedbackLog` 挂载时读 `?pattern=` 并高亮（ring + 蓝底）+ `scrollIntoView` 到匹配的 CounterExample 卡片，让审阅者当场确认"下一轮会被注入到 repair CLAUDE.md 的 pattern 就是这条"（北极星 #5）。
- 左侧导航的活体 chip 在 alert/warn tone 下自动把 NavLink 指向对应的预筛选子视图——Review chip `alert` 时 `to="/review?status=unresolvable"`、`warn` 时 `to="/review?status=pending"`、`default` 时回到 `/review`——与 StatCard drill-down 共用 query 约定，让"看到红色 chip 就 1 次点击落到 agent 放弃的 GAP 列表"成为从任意页面都成立的快捷路径。
- 未来新增 drill-down 链接时沿用相同约定：`?<filterName>=<value>`，命中则透传，否则忽略（宽松解析，架构契约优先）。

---

## 6. 项目结构

```
codemap_lite/
├── cli.py                          # Typer CLI
├── config/
│   ├── settings.py                 # Pydantic Settings (读取 config.yaml)
│   └── default_config.yaml         # 默认配置模板
├── parsing/
│   ├── base_plugin.py              # LanguagePlugin Protocol
│   ├── plugin_registry.py          # 插件发现
│   └── cpp/
│       ├── plugin.py               # CppPlugin
│       ├── call_graph.py           # 复用 AI4CParser CallGraphBuilder
│       └── symbol_extractor.py     # 复用 AI4CParser ASTParser
├── analysis/
│   ├── repair_orchestrator.py      # 修复编排器（并发池 + subprocess 管理 + 门禁验证）
│   ├── prompt_builder.py           # 模板化 prompt 构造
│   ├── feedback_store.py           # 泛化反例存储 + 相似性判断 + MD 文件更新
│   └── source_point_client.py      # codewiki_lite REST API 客户端
├── agent/
│   ├── icsl_tools.py               # 图查询 + 边写入 + 门禁 CLI 工具（复制到目标目录）
│   ├── claude_md_template.py       # CLAUDE.md 模板生成
│   ├── hooks/
│   │   ├── log_tool_use.py         # PostToolUse hook
│   │   └── log_notification.py     # Notification hook
│   └── templates/
│       ├── system_prompt.md        # Agent 角色定义模板
│       └── source_repair_prompt.md # Source 点修复 prompt 模板
├── graph/
│   ├── schema.py                   # 节点/关系类型
│   ├── neo4j_store.py              # Neo4j 读写
│   └── incremental.py              # 增量更新
├── api/
│   ├── app.py                      # FastAPI
│   └── routes/
│       ├── graph.py                # 图浏览
│       ├── review.py               # 评审 CRUD
│       └── analyze.py              # 分析触发 + 进度
└── pipeline/
    └── orchestrator.py             # 全流程编排（解析 → 修复 → 进度）
```

### 目标代码目录注入文件

修复 Agent 运行时，orchestrator 在目标代码目录生成以下临时文件：

```
目标代码目录/
├── CLAUDE.md                       # Agent 角色 + 工具说明 + 反例库引用
├── .claude/
│   └── settings.json               # Hook 配置
└── .icslpreprocess/
    ├── icsl_tools.py               # 图查询 + 边写入 + 门禁 CLI 工具
    ├── counter_examples.md          # 反例库（泛化后全量）
    ├── config.yaml                  # Neo4j 连接配置
    ├── hooks/
    │   ├── log_tool_use.py
    │   └── log_notification.py
    └── logs/                        # Agent 推理日志
        └── repair/{source_id}/
```

---

## 7. 增量更新策略

1. **文件变更检测**：SHA256 哈希对比（state.json + Neo4j File.hash）
2. **变更文件重解析**：删除旧 Function 节点及关联 CALLS 边 + UnresolvedCall，重新解析
3. **级联失效**：变更函数的 callers 中如有 LLM 修复的边指向旧函数 → 删除该 CALLS 边 + 对应 RepairLog，重新生成 UnresolvedCall
4. **重新获取 source 点**：每次增量前重新查询 codewiki_lite API
5. **重新修复**：对受影响的 source 可达路径重新运行修复 Agent（retry_count 重置）

---

## 8. REST API

```
# 图浏览
GET  /api/v1/files
GET  /api/v1/functions?file={path}
GET  /api/v1/functions/{id}
GET  /api/v1/functions/{id}/callers
GET  /api/v1/functions/{id}/callees
GET  /api/v1/functions/{id}/call-chain?depth=5

# Source 点
GET  /api/v1/source-points
GET  /api/v1/source-points/{id}/reachable

# 分析
POST /api/v1/analyze              # 触发全量/增量
POST /api/v1/analyze/repair       # 触发修复 Agent
GET  /api/v1/analyze/status       # 进度（轮询）

# 评审
GET    /api/v1/reviews
POST   /api/v1/reviews            # 标记边正确/错误
PUT    /api/v1/reviews/{id}
DELETE /api/v1/reviews/{id}

# 反例库
GET  /api/v1/feedback             # 浏览反例
POST /api/v1/feedback             # 新增反例（审阅标记错误时触发，架构 §5）
GET  /api/v1/stats                # 统计（含 unresolved_by_status 分桶：pending / unresolvable；含 calls_by_resolved_by 分桶：symbol_table / signature / dataflow / context / llm；含 total_feedback：反例库当前条目数，供左侧导航 Feedback 标签活体计数 chip 使用）
```

---

## 9. 技术栈

| 层 | 技术 |
|----|------|
| 语言解析 | tree-sitter + tree-sitter-cpp（MVP 只做 C/C++） |
| 后端 | Python 3.11+ / FastAPI |
| 图数据库 | Neo4j 5.x |
| 修复 Agent | opencode CLI / claudecode CLI（subprocess 调用） |
| CLI | Typer |
| 前端 | React 18 + TypeScript + Vite + Cytoscape.js + Tailwind |
| 配置 | config.yaml + 环境变量覆盖 |
| 部署 | 本地 pip install |

---

## 10. 配置管理

```yaml
# config.yaml
project:
  target_dir: "/path/to/target/code"

neo4j:
  uri: "bolt://localhost:7687"
  user: "neo4j"
  password: "${NEO4J_PASSWORD}"  # 环境变量覆盖

codewiki_lite:
  base_url: "http://localhost:8000"

agent:
  backend: "claudecode"          # "claudecode" | "opencode"
  max_concurrency: 5             # 并发池大小
  retry_failed_gaps: true        # 跨运行重试：下次运行时重置 unresolvable GAP 的 retry_count，重新尝试
  claudecode:
    command: "claude"
    args: ["-p", "--output-format", "text"]
  opencode:
    command: "opencode"
    args: ["-p"]

visualization:
  aggregation: "hierarchical"    # 使用 codewiki_lite 模块信息分层聚合
```

敏感信息（API key、数据库密码）通过环境变量注入，config.yaml 中用 `${VAR_NAME}` 语法引用。

---

## 11. 部署方式

本地 pip install：

```bash
pip install -e .
# 前置条件：Neo4j 5.x 已启动，codewiki_lite 已运行
# 前置条件：claudecode 或 opencode CLI 已安装并配置

# 全量分析
codemap-lite analyze --config config.yaml

# 增量分析
codemap-lite analyze --incremental --config config.yaml

# 仅修复
codemap-lite repair --config config.yaml

# 启动 API 服务
codemap-lite serve --config config.yaml

# 启动前端（开发模式）
cd frontend && npm run dev
```

---

## 12. 实施阶段

### Phase 1: 基础框架
- 项目结构 + pyproject.toml
- LanguagePlugin Protocol + CppPlugin
- 移植 AI4CParser（ASTParser + CallGraphBuilder + 前 3 层解析器）
- Neo4jStore + schema
- config.yaml 配置加载

### Phase 2: 修复 Agent
- codewiki_lite REST API 客户端
- Repair Orchestrator（并发池 + subprocess 管理 + 门禁验证）
- Prompt 模板 + CLAUDE.md 生成
- icsl_tools.py（图查询 + 边写入 + 门禁 CLI 工具）
- Hook 脚本（推理日志 + RepairLog）
- FeedbackStore（泛化反例 + 相似性合并 + MD 文件更新）
- 注入文件生成 + 清理
- 增量更新逻辑
- 进度打点（内存字典）

### Phase 3: API + 前端
- FastAPI REST API
- React 前端（调用图浏览 + 分层聚合 + 审阅 + 进度）

### Phase 4: 多语言扩展（后续）
- Java plugin (tree-sitter-java)
- Rust plugin (tree-sitter-rust)

---

## 13. 验证方案

1. 给定小型 C 项目（含间接调用），验证静态解析生成正确的直接调用边
2. 构造含函数指针/虚函数的代码，验证修复 Agent 能补全 CALLS 边
3. 修改文件后增量运行，验证仅受影响子图被重建
4. 前端标记边为错误，验证反例生成 + 下次修复时反例被注入
5. 切换 LLM 后端（claudecode ↔ opencode），验证修复流程正常

---

## 关键复用文件

| 来源 | 文件 | 复用内容 |
|------|------|---------|
| AI4CParser | `src/core/ast_parser.py` | tree-sitter 解析 + 函数提取 |
| AI4CParser | `src/core/call_graph_builder.py` | 调用边构建 |
| AI4CParser | `src/analysis/indirect_call_resolver.py` | 3 层静态间接调用解析 |
| AI4CParser | `src/analysis/class_hierarchy_analyzer.py` | 虚函数 CHA 解析 |
| AI4CParser | `src/analysis/data_flow_analyzer.py` | 变量赋值追踪 |
| codemap | `codemap/graph/neo4j.py` | Neo4j 存储层 |
| codemap | `codemap/core/types.py` | 节点/关系类型定义 |

---

## 附录：架构决策记录 (ADR)

| # | 决策点 | 结论 | 决策方式 |
|---|--------|------|---------|
| 1 | 图粒度 | 函数级 Call Graph（非 CFG） | AI推荐 → 人确认 |
| 2 | 核心输出 | 仅 CALLS 边（无 FLOWS_TO） | 人主动简化 |
| 3 | 修复范围 | 仅 Source 可达路径 | AI推荐 → 人确认 |
| 4 | 解析策略 | 两阶段分离（静态 + LLM） | AI推荐 → 人确认 |
| 5 | 终止条件 | 不限深度，无源码时停止 | 人自定义 |
| 6 | 边属性 | 仅 resolved_by（无 confidence） | 人追问后简化 |
| 7 | LLM 后端 | opencode CLI / claudecode CLI | 人指定 |
| 8 | Agent 形态 | Subprocess CLI（非 SDK API） | 人指定 |
| 9 | Agent 粒度 | 每个 source 点一个 Agent（Agent 自主 BFS） | 人修正 |
| 9b | CLI 交互 | 单次调用（-p 模式，每个 source 点一次） | 人选择 |
| 10 | Agent 工作目录 | 目标代码目录 | 人选择 |
| 11 | Neo4j 写入 | Agent 调用 icsl_tools.py write-edge | 人选择 |
| 12 | 并发控制 | 固定并发池（max 3-5） | 人选择 |
| 13 | 超时 | 不限时 | 人指定 |
| 14 | 推理捕获 | CLI Hook → 日志 + RepairLog | 人确认 |
| 15 | 上下文注入 | CLAUDE.md 机制 | 人确认 |
| 16 | 反例库格式 | Markdown 文件（CLAUDE.md 引用） | 人选择 |
| 17 | 反例注入 | 泛化去重后全量注入 | 人设计 |
| 18 | 反例相似性 | LLM 判断 + 总结合并 | 人设计 |
| 19 | LLM 输出格式 | Tool Use / Function Calling | AI推荐 → 人确认 |
| 20 | 可视化 | 全量渲染 + 分层聚合（codewiki_lite 模块） | 人设计 |
| 21 | 并发粒度 | Source 点级并发 | 人选择 |
| 22 | 冲突处理 | 乐观锁（先到先得） | AI推荐 → 人确认 |
| 23 | Source 接入 | REST API 调用 | 人选择 |
| 24 | 审阅粒度 | 以边为粒度 | AI推荐 → 人确认 |
| 25 | 进度感知 | 打点 + 轮询 | 人选择 |
| 26 | 多语言 | MVP 只做 C/C++ | AI推荐 → 人确认 |
| 27 | 配置管理 | config.yaml | 人选择 |
| 28 | 增量触发 | CLI + REST API 都支持 | 人确认 |
| 29 | 部署方式 | 本地 pip install | 人选择 |
| 30 | 失败处理 | 跳过 + 记录，下次重试 | 人选择 |
| 31 | Agent 遍历 | 一次性获取可达子图，Agent 自主 BFS | 人选择 |
| 32 | 动态可达 | Agent 循环查询（修完一批再查） | 人选择 |
| 33 | 完整性判定 | 门禁验证（check-complete） | 人指定 |
| 34 | 门禁未通过 | 重启 source 级 Agent（已修 GAP 自动跳过） | 人选择 |
| 35 | 重试上限 | 每个 GAP 最多 3 次（非 source 级） | 人指定 |
| 36 | 门禁时机 | 立即门禁 + 重启（source 内串行） | 人选择 |
| 37 | 并发模型 | source 间并发，source 内串行 | 人确认 |
| 38 | GAP 存储 | UnresolvedCall 节点（含候选列表，标注为参考） | 人选择 |
| 39 | 候选目标 | 提供但标注为参考，Agent 可自主发现新目标 | 人确认 |
| 40 | 图状态获取 | Agent 通过 icsl_tools.py 自主查询 Neo4j | 人选择 |
| 41 | 工具组织 | 单文件 CLI（icsl_tools.py，子命令区分） | 人选择 |
| 42 | Neo4j 连接 | icsl_tools.py 读 config.yaml | 人选择 |
| 43 | 解析范围 | 全量解析 + 按需修复（仅 source 可达 GAP） | 人修正 |
| 44 | 重复修复 | Agent 自主判断跳过（检查边是否已存在） | 人选择 |
| 45 | 注入文件清理 | 每次重新生成，Agent 完成后清理 | 人选择 |
| 46 | 进度存储 | 内存字典 + 前端轮询 | 人选择 |
| 47 | 反例泛化 | 后端自动泛化（异步）+ CLI subprocess | 人选择 |
| 48 | 反例泛化触发 | 前端审阅后异步触发 | 人选择 |
| 49 | 增量 GAP 处理 | 重新解析 + 重新修复 | 人选择 |
| 50 | CLI 命令 | analyze / repair / serve / status | 人确认 |
| 51 | RepairLog 关联 | 属性引用（caller_id + callee_id + location），无额外关系 | 人选择 |
| 52 | 进度通信 | 文件通信（Hook 写 progress.json，Orchestrator 读） | 人确认 |
| 53 | 推理记录 | JSONL 日志文件（每个 GAP 一个），摘要写入 RepairLog | 人确认 |
| 54 | Config 结构 | claudecode/opencode 嵌套在 agent 下 | 人选择 |
| 55 | 审阅后处理 | 立即删除边 + 重建 UnresolvedCall + 触发重新修复 | 人选择 |
| 56 | valid 字段 | 移除（错误边直接删除，不保留 invalid 状态） | 推导自 #55 |
