# ADR-0002: 第二轮 GAP 分析 — 架构文档 vs 实施计划

## Status: Accepted

## Context

ADR-0001 修正了 25 个 GAP 后，再次逐行对比 architecture.md 和 implementation-plan.md，发现以下遗漏。

---

## 新发现的 GAP

### G1: `state.json` 文件缓存（增量更新的持久化状态）

- **架构**: Section 7 line 405 — "SHA256 哈希对比（**state.json** + Neo4j File.hash）"
- **计划**: 2.7 只提到 SHA256 检测，未提及 state.json 文件的生成、存储位置、格式
- **影响**: 没有 state.json，增量更新无法在 Neo4j 之外快速判断哪些文件变更（每次都要查 Neo4j 的 File.hash）
- **修正**:
  - FileCache (1.6) 需要生成并维护 `state.json`（存储 `{file_path: sha256_hash}` 映射）
  - 存储位置: 项目工作目录（如 `.icslpreprocess/state.json`）或 config 指定路径
  - 增量更新时: 先对比 state.json vs 当前文件系统 → 得到变更列表 → 再操作 Neo4j

### G2: `HAS_GAP` 关系未在实施计划中体现

- **架构**: Section 4 line 252 — 关系类型 `HAS_GAP: Function → UnresolvedCall`
- **计划**: 1.7 的 schema 验收标准只提到 CALLS 边和索引，未提及 HAS_GAP 和 IS_SOURCE 关系的创建
- **影响**: 没有 HAS_GAP 关系，`query-reachable` 无法从 Function 节点遍历到其 UnresolvedCall；没有 IS_SOURCE，无法从 Function 找到对应 SourcePoint
- **修正**: 1.7 验收标准补充: 实现全部 4 种关系类型（DEFINES, CALLS, HAS_GAP, IS_SOURCE）

### G3: `File.primary_language` 属性

- **架构**: Section 4 line 240 — File 节点属性包含 `primary_language`
- **计划**: 1.6 FileScanner 只提到 file_path 和 SHA256 hash
- **影响**: 多语言扩展时需要按语言过滤文件；PluginRegistry 需要知道文件语言来分派给正确的 Plugin
- **修正**: FileScanner 根据文件扩展名设置 primary_language（.cpp/.cc/.h → "cpp"）

### G4: `UnresolvedCall.var_name` 和 `var_type` 属性

- **架构**: Section 4 line 243 — UnresolvedCall 属性包含 `var_name, var_type`
- **计划**: 2.2 的 query-reachable 输出 schema 未包含 var_name 和 var_type
- **影响**: Agent 修复时缺少变量名和类型信息，降低修复准确率（例如知道 `callback` 是 `std::function<void(int)>` 类型能大幅缩小候选范围）
- **修正**:
  - 1.5 间接调用解析时提取 var_name 和 var_type
  - 2.2 query-reachable 输出 schema 补充这两个字段

### G5: `RepairLog` 完整属性

- **架构**: Section 4 line 244 — RepairLog 属性: `caller_id, callee_id, call_location, repair_method, llm_response, timestamp, reasoning_summary`
- **计划**: 2.2 write-edge 只提到创建 RepairLog，未明确写入哪些属性
- **影响**: RepairLog 缺少 `repair_method`（应为 "llm"）、`llm_response`（原始 LLM 输出）、`timestamp`
- **修正**: write-edge 命令需要接受额外参数或从上下文推断:
  - `repair_method`: 固定为 "llm"（Agent 修复）
  - `llm_response`: Agent 可传入（或从 JSONL 提取）
  - `timestamp`: write-edge 执行时自动生成
  - `reasoning_summary`: 后续由 Orchestrator 从 JSONL 补充

### G6: `CALLS.call_type` 属性在 write-edge 中的传递

- **架构**: Section 4 line 261 — CALLS 边属性包含 `call_type: "direct" | "indirect" | "virtual"`
- **计划**: 2.2 write-edge 命令参数只有 `--caller --callee --location`，缺少 `--call-type`
- **影响**: Agent 写入的 CALLS 边缺少 call_type，无法区分修复的是间接调用还是虚函数调用
- **修正**: write-edge 增加 `--call-type` 参数（Agent 从 UnresolvedCall.call_type 获取）

### G7: 审阅操作 — "手动添加边" 和 "手动删除边"

- **架构**: Section 5 line 307 — 审阅操作包含 "标记正确 / 标记错误 / **手动添加边** / **手动删除边**"
- **计划**: 3.5 审阅 API 只覆盖了标记正确/错误，未覆盖手动添加和删除
- **影响**: 审阅者无法手动补充 Agent 遗漏的边，也无法删除明显错误的边（只能标记错误后等重修复）
- **修正**: 审阅 API 补充:
  - `POST /api/v1/edges` — 手动添加 CALLS 边（resolved_by="manual"）
  - `DELETE /api/v1/edges/{id}` — 手动删除 CALLS 边

### G8: `pipeline/orchestrator.py` 的全流程编排角色

- **架构**: Section 6 line 378 — `pipeline/orchestrator.py` 注释为 "全流程编排（解析 → 修复 → 进度）"
- **计划**: 1.8 的 pipeline orchestrator 只做 "scan → parse → store"；2.5 的 repair_orchestrator 只做修复
- **影响**: 缺少一个顶层编排器串联 "解析 → 获取 source 点 → 修复 → 进度汇报" 的完整流程。CLI `analyze` 命令和 API `POST /analyze` 需要调用这个顶层编排器
- **修正**: pipeline/orchestrator.py 应该是顶层入口，调用:
  1. FileScanner + CppPlugin（解析）
  2. Neo4jStore（存储）
  3. SourcePointClient（获取 source 点）
  4. RepairOrchestrator（修复）
  5. ProgressTracker（进度汇报）

### G9: `POST /api/v1/analyze` 区分全量/增量

- **架构**: Section 8 line 429 — `POST /api/v1/analyze # 触发全量/增量`
- **计划**: 3.4 只说 "POST 触发 → 后台任务启动"，未说明如何区分全量和增量
- **影响**: API 调用者无法指定是全量还是增量分析
- **修正**: POST body 或 query param 包含 `mode: "full" | "incremental"`

### G10: `GET /api/v1/functions/{id}` 单函数详情 API

- **架构**: Section 8 line 419 — `GET /api/v1/functions/{id}`
- **计划**: 3.2 列出了 `/functions`, `/functions/{id}/callers`, `/functions/{id}/callees`, `/functions/{id}/call-chain` 但遗漏了 `/functions/{id}` 本身
- **影响**: 前端 Node Inspector 无法获取单个函数的详细信息（signature, file_path, start_line, end_line）
- **修正**: 3.2 补充 `GET /api/v1/functions/{id}` 返回函数详情

### G11: `GET /api/v1/functions?file={path}` 按文件过滤

- **架构**: Section 8 line 418 — `GET /api/v1/functions?file={path}`
- **计划**: 3.2 只写了 `/functions`，未明确支持 `?file=` 查询参数
- **影响**: 前端 FunctionBrowser 的 "文件树 → 函数列表" 交互无法实现
- **修正**: 3.2 明确 `/functions` 支持 `?file={path}` 过滤

### G12: 前端 "Node Inspector" 组件

- **架构**: Section 1 line 26 — Layer 5 包含 "Node Inspector"
- **计划**: 3.6 页面列表中没有 Node Inspector
- **影响**: 用户点击调用图中的节点后无法查看详情（函数签名、文件位置、callers/callees 数量）
- **修正**: Node Inspector 可能是 CallGraphView 的子组件（侧边栏），而非独立页面。在 3.6 验收标准中补充: CallGraphView 包含节点详情面板

### G13: `agent/templates/` 目录中的两个模板文件

- **架构**: Section 6 lines 365-366 — `templates/system_prompt.md` 和 `templates/source_repair_prompt.md`
- **计划**: 2.3 只提到 `claude_md_template.py` 和 `prompt_builder.py`，未提及这两个 .md 模板文件
- **影响**: Prompt 构建逻辑硬编码在 Python 中，难以迭代调整 prompt 内容
- **修正**: 2.3 应产出两个 Jinja2/字符串模板文件:
  - `system_prompt.md` — Agent 角色定义（通用部分）
  - `source_repair_prompt.md` — 每个 source 点的具体修复指令（含 source_id、工具说明、终止条件）

### G14: 反例泛化使用 "CLI subprocess"

- **架构**: ADR #47 line 627 — "反例泛化: 后端自动泛化（异步）+ **CLI subprocess**"
- **计划**: 2.6 FeedbackStore 只说 "LLM 相似性判断"，未明确泛化过程也是通过 CLI subprocess 调用 LLM
- **影响**: FeedbackStore 的实现方式不明确 — 是直接调用 OpenAI API，还是也通过 opencode/claude subprocess？
- **修正**: 明确 FeedbackStore 的 LLM 调用方式。考虑到泛化是轻量操作（单次 prompt），建议直接用 OpenAI 兼容 SDK 调用 DashScope API（而非启动完整 CLI subprocess），但需要在计划中明确这个决策

### G15: `resolved_by` 的完整枚举值

- **架构**: Section 4 line 260 — `resolved_by: "symbol_table" | "signature" | "dataflow" | "context" | "llm"`
- **计划**: 各处只提到 "symbol_table" 和 "llm"，遗漏了 "signature"、"dataflow"、"context"
- **影响**: 3 层静态解析器（1.5）解析成功的间接调用，其 CALLS 边的 resolved_by 应分别标记为 signature/dataflow/context，而非统一标记为某个值
- **修正**: 1.5 的每层解析器成功解析时，设置对应的 resolved_by 值

### G16: 审阅 API — `PUT /api/v1/reviews/{id}` 更新审阅

- **架构**: Section 8 line 436 — `PUT /api/v1/reviews/{id}`
- **计划**: 3.5 只提到 POST（创建）和标记正确/错误，未提及 PUT（更新）和 DELETE
- **影响**: 审阅者无法修改已提交的审阅（例如改变判定）
- **修正**: 3.5 补充完整 CRUD: GET list, POST create, PUT update, DELETE remove

---

## 与 ADR-0001 的关系

ADR-0001 聚焦于 "计划中有但不够详细" 的 GAP。本 ADR 聚焦于 "架构中有但计划中完全遗漏" 的 GAP。两者互补。

## 优先级

| 级别 | GAP | 原因 |
|------|-----|------|
| Critical | G2 (HAS_GAP/IS_SOURCE 关系) | 没有这些关系，query-reachable 无法工作 |
| Critical | G8 (pipeline orchestrator 全流程) | CLI 和 API 的入口点不完整 |
| High | G1 (state.json) | 增量更新的性能依赖 |
| High | G4 (var_name/var_type) | 影响 Agent 修复准确率 |
| High | G5 (RepairLog 完整属性) | 审阅功能依赖 |
| High | G6 (call_type in write-edge) | 数据完整性 |
| High | G7 (手动添加/删除边) | 审阅功能完整性 |
| High | G15 (resolved_by 枚举) | 静态解析结果的可追溯性 |
| Medium | G3 (primary_language) | 多语言扩展准备 |
| Medium | G9 (全量/增量区分) | API 可用性 |
| Medium | G10, G11 (函数 API) | 前端功能 |
| Medium | G12 (Node Inspector) | 前端交互 |
| Medium | G13 (模板文件) | 可维护性 |
| Medium | G14 (反例泛化 LLM 调用方式) | 实现方式决策 |
| Low | G16 (PUT/DELETE reviews) | API 完整性 |

## Decision: G14 反例泛化 LLM 调用方式

**决策**: FeedbackStore 的泛化和相似性判断直接使用 OpenAI 兼容 SDK 调用 DashScope API（`settings-alibaba.json` 中的配置），而非启动 CLI subprocess。

**理由**:
- 泛化是单次 prompt 调用，不需要文件读取/搜索能力
- CLI subprocess 启动开销大（加载 CLAUDE.md、初始化工具），不适合轻量操作
- DashScope 兼容 OpenAI SDK，直接 `openai.ChatCompletion.create()` 即可

**Config 补充**:
```yaml
feedback:
  model: "qwen-plus"  # 泛化用的模型
  base_url: "${OPENAI_BASE_URL}"
  api_key: "${OPENAI_API_KEY}"
```
