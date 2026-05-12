# codemap-lite 分步实施计划

> 基于 `docs/architecture.md` 制定，CastEngine 作为端到端验证目标。

## 实施顺序与依赖

```
1.1 项目骨架
 ↓
1.2 Plugin Protocol ──→ 1.3 符号提取 ──→ 1.4 直接调用 ──→ 1.5 间接调用解析
                                                              ↓
1.6 FileScanner ─────────────────────────────────────→ 1.8 Pipeline 整合
                                                              ↑
1.7 Neo4j Store ──────────────────────────────────────────────┘
 ↓
2.1 Source Client ──→ 2.5 Orchestrator ──→ 2.8 CLI 命令
                          ↑
2.2 icsl_tools ───────────┤
2.3 Prompt Builder ───────┤
2.4 Hook 脚本 ────────────┘
2.6 FeedbackStore ──→ 2.5 (反例注入)
2.7 增量更新 ──→ 2.5 (增量修复触发)
 ↓
3.1 FastAPI ──→ 3.2-3.5 API Routes ──→ 3.6 Frontend
```

---

## Phase 1: 基础框架（解析 + 存储）

### 1.1 项目骨架
- **做什么**: pyproject.toml, cli.py (Typer), config/settings.py (Pydantic), default_config.yaml
- **复用**: 无（新建）
- **测试**: `codemap-lite --help` 正常输出；config.yaml 加载 + 环境变量覆盖（`${VAR_NAME}` 插值）
- **文件**: `codemap_lite/cli.py`, `codemap_lite/config/settings.py`
- **验收标准**:
  - Pydantic Settings 覆盖完整 config 结构（project, neo4j, codewiki_lite, agent, visualization）
  - `${VAR_NAME}` 语法通过 `os.path.expandvars()` 解析
  - CLI 命令: analyze, repair, status, serve

### 1.2 LanguagePlugin Protocol + Registry
- **做什么**: `base_plugin.py` 定义 Protocol（parse_file, extract_symbols, build_calls）；`plugin_registry.py` 自动发现插件
- **复用**: 无（新设计）
- **测试**: 注册 mock plugin → registry 能发现并调用
- **验收标准**: Protocol 必须语言无关（无 C++ 特定方法），验证方式：能 sketch 出 JavaPlugin 接口而不需改 Protocol

### 1.2.5 AI4CParser Adapter Layer
- **做什么**: 从 AI4CParser 复制核心文件，适配为 LanguagePlugin Protocol 实现
- **策略**: Copy + Adapt（非 submodule，避免版本耦合）
- **适配清单**:
  - `AI4CParser/src/core/ast_parser.py` → `parsing/cpp/symbol_extractor.py`（输出 Function dataclass）
  - `AI4CParser/src/core/call_graph_builder.py` → `parsing/cpp/call_graph.py`（输出 CallEdge dataclass）
  - `AI4CParser/src/analysis/indirect_call_resolver.py` → `parsing/cpp/plugin.py`（输出 UnresolvedCall dataclass）
  - `codemap/codemap/graph/neo4j.py` → `graph/neo4j_store.py`（适配新 schema）
- **测试**: 适配后的模块能通过原 AI4CParser 的测试用例

### 1.3 CppPlugin — 符号提取
- **做什么**: tree-sitter 解析 C/C++ → 提取 Function 节点（name, signature, file_path, start_line, end_line, body_hash）
- **复用**: `AI4CParser/src/core/ast_parser.py`（ASTParser 类，tree-sitter 解析逻辑）
- **测试**: 给定 CastEngine 的 `cast_session_impl.cpp` → 提取出所有函数定义（ProcessSetUp, ProcessPlay 等）
- **文件**: `codemap_lite/parsing/cpp/symbol_extractor.py`

### 1.4 CppPlugin — 直接调用构建
- **做什么**: 从 AST 提取直接函数调用边（caller → callee）
- **复用**: `AI4CParser/src/core/call_graph_builder.py`
- **测试**: CastEngine `cast_session_impl.cpp` 中 `CastDeviceDataManager::GetInstance()` 等直接调用被正确识别
- **文件**: `codemap_lite/parsing/cpp/call_graph.py`

### 1.5 CppPlugin — 3 层静态间接调用解析
- **做什么**: 签名匹配 → 数据流追踪 → 上下文推断，尽力解析间接调用
- **复用**: `AI4CParser/src/analysis/indirect_call_resolver.py`, `data_flow_analyzer.py`, `class_hierarchy_analyzer.py`
- **测试**: CastEngine `stateProcessor_` 数组 → 签名匹配能识别部分候选；虚函数 → CHA 能列出实现类
- **文件**: `codemap_lite/parsing/cpp/plugin.py`（整合）
- **验收标准**:
  - 每层解析成功时设置对应 resolved_by: "signature" / "dataflow" / "context"
  - 未解析的间接调用生成 UnresolvedCall，包含 var_name 和 var_type 属性
  - 生成 HAS_GAP 关系（Function → UnresolvedCall）

### 1.6 FileScanner + FileCache
- **做什么**: 递归扫描目标目录，SHA256 哈希缓存，识别变更文件
- **复用**: 无（简单实现）
- **测试**: 扫描 CastEngine → 返回所有 .cpp/.h 文件列表；修改文件后 → 检测到变更
- **验收标准**:
  - 生成并维护 `state.json`（`{file_path: sha256_hash}` 映射），存储在 `.icslpreprocess/state.json`
  - 根据文件扩展名设置 `primary_language`（.cpp/.cc/.cxx/.h/.hpp → "cpp"）
  - 增量模式: 对比 state.json vs 当前文件系统 → 返回变更文件列表

### 1.7 Neo4j Schema + Store + QueryEngine
- **做什么**: schema.py（节点/关系类型定义）、neo4j_store.py（CRUD）、query_engine.py（复杂图查询）、索引创建
- **复用**: `codemap/codemap/graph/neo4j.py`（连接管理 + 基础 CRUD）、`codemap/codemap/core/types.py`
- **测试**: 写入 Function 节点 → 读回验证；写入 CALLS 边 → 查询 callers/callees
- **文件**: `codemap_lite/graph/schema.py`, `codemap_lite/graph/neo4j_store.py`, `codemap_lite/graph/query_engine.py`
- **验收标准**:
  - 全部 4 种关系类型: DEFINES (File→Function), CALLS (Function→Function), HAS_GAP (Function→UnresolvedCall), IS_SOURCE (Function→SourcePoint)
  - CALLS 边属性: resolved_by ("symbol_table"|"signature"|"dataflow"|"context"|"llm"|"manual"), call_type ("direct"|"indirect"|"virtual"), call_file, call_line
  - 7 个索引全部创建: idx_file_hash, idx_function_file, idx_function_sig, idx_source_kind, idx_calls_resolved, idx_gap_status, idx_gap_caller
  - QueryEngine 支持: reachable subgraph, call-chain with depth, callers/callees traversal

### 1.8 Pipeline 整合（全量解析 + 全流程编排）
- **做什么**: `pipeline/orchestrator.py` 作为顶层入口，串联完整流程:
  1. FileScanner + CppPlugin（解析）
  2. Neo4jStore（存储 Function + CALLS + UnresolvedCall + DEFINES + HAS_GAP）
  3. SourcePointClient（获取 source 点 + 创建 IS_SOURCE 关系）
  4. RepairOrchestrator（修复，Phase 2 实现）
  5. ProgressTracker（进度汇报）
- **CLI**: `analyze` 命令调用此 orchestrator；`--incremental` flag 切换增量模式
- **测试**: `codemap-lite analyze --config config.yaml` 对 CastEngine → Neo4j 中有 Function 节点 + CALLS 边 + UnresolvedCall 节点 + DEFINES/HAS_GAP/IS_SOURCE 关系

---

## Phase 2: 修复 Agent

### 2.1 codewiki_lite REST Client
- **做什么**: `source_point_client.py` — 调用 codewiki_lite API 获取 source 点列表
- **测试**: mock HTTP → 返回 source 点列表；写入 Neo4j SourcePoint 节点
- **验收标准**:
  - 定义 Pydantic 响应模型（含 entry_point_kind, reason, function_id, module 层级信息）
  - 映射 API 响应 → SourcePoint 节点属性
  - 保存 module 层级信息供可视化聚合使用

### 2.2 icsl_tools.py（Agent 侧 CLI 工具）
- **做什么**: 子命令 `query-reachable`, `write-edge`, `check-complete`；读 config.yaml 连接 Neo4j
- **测试**:
  - `query-reachable --source <id>` → 返回可达子图 JSON
  - `write-edge --caller <id> --callee <id> --call-type <type> --location <file:line>` → Neo4j 中新增 CALLS 边 + RepairLog + 删除 UnresolvedCall + 删除 HAS_GAP 关系
  - `check-complete --source <id>` → 返回是否还有 pending GAP
- **文件**: `codemap_lite/agent/icsl_tools.py`
- **验收标准**:
  - query-reachable 输出 schema: `{nodes: [{id, name, signature, file_path}], edges: [{source, target, resolved_by, call_type}], unresolved: [{id, caller_id, call_expression, call_file, call_line, call_type, var_name, var_type, candidates, source_code_snippet}]}`
  - write-edge 参数: `--caller`, `--callee`, `--call-type` (indirect|virtual), `--call-file`, `--call-line`
  - write-edge 创建 RepairLog 含完整属性: caller_id, callee_id, call_location, repair_method="llm", timestamp=now
  - write-edge 使用结构化 location（call_file + call_line 两个属性）

### 2.3 Prompt Builder + CLAUDE.md 模板
- **做什么**: 根据 source 点信息生成 Agent prompt；生成目标目录的 CLAUDE.md
- **测试**: 给定 source 点 → 生成的 prompt 包含正确的工具说明 + 反例引用 + 终止条件
- **文件**: `codemap_lite/agent/claude_md_template.py`, `codemap_lite/analysis/prompt_builder.py`
- **模板文件**:
  - `agent/templates/system_prompt.md` — Agent 角色定义（通用部分，Jinja2 模板）
  - `agent/templates/source_repair_prompt.md` — 每个 source 点的具体修复指令（含 source_id、工具说明、终止条件）
- **验收标准**: 生成的 prompt 必须包含全部 5 个终止条件:
  1. 到达系统库/标准库函数（无源码）
  2. 搜索整个代码库找不到函数实现
  3. 调用链形成环（递归检测）
  4. 所有可达 UnresolvedCall 已处理
  5. 到达 sink 点后继续追踪（发现所有可达路径）

### 2.4 Hook 脚本
- **做什么**: `log_tool_use.py`（PostToolUse → JSONL 日志）、`log_notification.py`（Notification → progress.json）
- **测试**: 模拟 hook 输入 → 验证 JSONL 追加 + progress.json 更新
- **文件**: `codemap_lite/agent/hooks/`
- **验收标准**:
  - 文档化 stdin JSON schema（PostToolUse: tool_name, params, result; Notification: message）
  - progress.json schema: `{fixed_gaps: int, total_gaps: int, current_gap_id: str}`
  - JSONL 格式: 每行含 tool_name, params, result_summary, timestamp

### 2.5 Repair Orchestrator（核心）
- **做什么**:
  - asyncio.Semaphore 并发池（max=5）
  - 为每个 source 点启动 CLI subprocess
  - 注入文件生成（CLAUDE.md, .claude/settings.json, .icslpreprocess/）
  - **备份/恢复已有 CLAUDE.md**（目标目录可能已有）
  - 门禁验证（GateChecker 独立类）
  - 重试逻辑（GAP 级别，max 3）
  - SourcePoint 状态机: pending → running → complete | partial_complete
  - 进度追踪（内存字典，每 2s 轮询 progress.json）
  - Agent 完成后: 解析 JSONL → 提取 reasoning_summary → 更新 RepairLog
  - Agent 完成后清理注入文件 + 恢复备份
  - **config 驱动命令构造**: 从 `agent.backend` 读取 command + args
- **测试**:
  - mock subprocess → 验证并发控制（不超过 max）
  - mock check-complete 返回有残留 → 验证重试
  - 验证注入文件生成 + 清理 + 备份恢复
  - 验证 SourcePoint 状态转换
  - 验证 reasoning_summary 提取
- **文件**: `codemap_lite/analysis/repair_orchestrator.py`, `codemap_lite/analysis/gate_checker.py`

### 2.6 FeedbackStore（反例库）
- **做什么**: 泛化反例、LLM 相似性判断、合并/新增、更新 counter_examples.md
- **LLM 调用方式**: 直接使用 OpenAI 兼容 SDK 调用 DashScope API（非 CLI subprocess，因为泛化是轻量单次 prompt）
- **测试**: 添加反例 → MD 文件更新；添加相似反例 → 合并而非重复
- **文件**: `codemap_lite/analysis/feedback_store.py`
- **验收标准**:
  - 提供异步触发接口（供审阅 API 通过 FastAPI BackgroundTasks 调用）
  - config 中新增 `feedback` section: model, base_url, api_key

### 2.7 增量更新（5 步级联）
- **做什么**: `incremental.py` — 完整 5 步级联:
  1. SHA256 哈希检测变更文件
  2. 删除旧 Function 节点 + 关联 CALLS/UnresolvedCall → 重新解析
  3. 级联失效: 查找 resolved_by=llm 且指向变更函数的 CALLS 边 → 删除 + 重建 UnresolvedCall
  4. 重新获取 source 点（调用 codewiki_lite API）
  5. 识别受影响 source 路径 → 触发重修复
- **额外**: 若 `retry_failed_gaps=true`，启动时重置所有 unresolvable GAP 为 pending
- **测试**: 修改 CastEngine 某文件 → 仅该文件的 Function/CALLS 被重建；依赖它的 LLM 边被失效
- **文件**: `codemap_lite/graph/incremental.py`

### 2.8 CLI 命令补全
- **做什么**: `repair`, `status`, `serve` 命令 + `analyze --incremental` flag
- **测试**: 所有 CLI 命令可执行；`--incremental` 连接到 incremental.py 逻辑

---

## Phase 3: API + 前端

### 3.1 FastAPI 基础
- **做什么**: app.py, CORS, 健康检查
- **测试**: `GET /health` → 200

### 3.2 图浏览 API
- **做什么**: `/api/v1/files`, `/functions?file={path}`, `/functions/{id}`, `/functions/{id}/callers`, `/functions/{id}/callees`, `/functions/{id}/call-chain?depth=5`
- **测试**: 解析 CastEngine 后 → API 返回正确的函数列表和调用关系；`?file=` 过滤正常工作

### 3.3 Source 点 API
- **做什么**: `/api/v1/source-points`, `/source-points/{id}/reachable`
- **测试**: 返回 source 点列表 + 可达子图

### 3.4 分析触发 API
- **做什么**: `POST /analyze` (body: `{mode: "full"|"incremental"}`), `POST /analyze/repair`, `GET /analyze/status`
- **测试**: POST 触发 → 后台任务启动 → status 轮询返回进度；mode 参数正确切换全量/增量

### 3.5 审阅 API
- **做什么**: 完整 CRUD:
  - `GET /api/v1/reviews` — 列表（支持过滤 resolved_by=llm 且未审阅的）
  - `POST /api/v1/reviews` — 标记正确/错误 + 触发反例生成 + 重建 UnresolvedCall + 异步触发重修复
  - `PUT /api/v1/reviews/{id}` — 更新审阅判定
  - `DELETE /api/v1/reviews/{id}` — 删除审阅
  - `POST /api/v1/edges` — 手动添加 CALLS 边（resolved_by="manual"）
  - `DELETE /api/v1/edges/{id}` — 手动删除 CALLS 边
- **测试**: 标记边错误 → CALLS 边被删除 → UnresolvedCall 重建 → 反例库更新 → 自动触发该 source 点 Agent 重修复；手动添加边 → 新 CALLS 边 resolved_by="manual"

### 3.5b 反例 + 统计 API
- **做什么**: `GET /api/v1/feedback`（反例库浏览）、`GET /api/v1/stats`（统计概览）
- **测试**: 返回反例列表；返回统计数据（source 点数、GAP 数、修复率）

### 3.6 React 前端
- **做什么**: Vite + React 18 + TypeScript + Tailwind + Cytoscape.js
- **页面**: Dashboard, SourcePointList, FunctionBrowser, CallGraphView (含 Node Inspector 侧边栏), ReviewQueue, FeedbackLog
- **测试**: Cypress E2E — 浏览调用图 → 点击节点查看详情 → 点击边 → 审阅 → 标记
- **验收标准**:
  - CallGraphView 使用 Cytoscape.js compound nodes 实现分层聚合（模块信息来自 codewiki_lite）
  - Node Inspector 作为 CallGraphView 子组件（侧边栏），展示函数签名、文件位置、callers/callees 数量
  - 两种浏览模式: 从 source 出发 / 从函数浏览

---

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| AI4CParser 代码与新架构不兼容 | Phase 1 延迟 | 先写 adapter 层，逐步替换 |
| CastEngine 解析耗时过长 | E2E 测试慢 | 支持子目录过滤，E2E 可只解析单模块 |
| LLM Agent 修复不稳定 | E2E-3 不可重复 | 用 mock subprocess 做确定性测试；真实 Agent 测试标记为 @slow |
| Neo4j 并发写入冲突 | Phase 2 数据不一致 | icsl_tools.py 写入前检查边是否存在（乐观锁） |
| codewiki_lite 不可用 | Phase 2 阻塞 | 支持 mock source 点列表（JSON 文件） |
