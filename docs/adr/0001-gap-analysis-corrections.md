# ADR-0001: 架构文档与实施计划 GAP 分析及修正

## Status: Accepted

## Context

对比 `docs/architecture.md` 和 `docs/implementation-plan.md`，发现 25 个差距。本 ADR 记录 Critical 和 High 级别的 GAP 及其修正决策。

## Critical Gaps (影响正确性)

### C1: UnresolvedCall 跨运行重试重置
- **问题**: 计划未覆盖 `retry_failed_gaps: true` 时重置 unresolvable GAP 的逻辑
- **修正**: 在 Pipeline Orchestrator 启动时，若 config `retry_failed_gaps=true`，重置所有 `status=unresolvable` 的 UnresolvedCall 为 `pending, retry_count=0`

### C2: Agent 注入文件 — 备份/恢复已有 CLAUDE.md
- **问题**: 目标目录可能已有 CLAUDE.md，直接覆盖会破坏用户项目
- **修正**: Orchestrator 注入前检查并备份已有文件，Agent 完成后恢复原文件

### C3: RepairLog.reasoning_summary 从 JSONL 提取
- **问题**: 计划中 RepairLog 缺少推理摘要
- **修正**: Agent 完成后，Orchestrator 解析 JSONL 日志，提取关键推理步骤写入 RepairLog.reasoning_summary

### C4: 增量更新 5 步级联
- **问题**: 计划仅一句"级联失效"，未分解 5 步
- **修正**: 拆分为: (1) SHA256 检测 → (2) 删旧节点+重解析 → (3) 失效 LLM 边(resolved_by=llm 且指向变更函数) → (4) 重获取 source 点 → (5) 触发受影响 source 重修复

### C5: 审阅标记错误后自动触发重修复
- **问题**: 计划只到"重建 UnresolvedCall"，缺少自动重修复
- **修正**: 审阅 API 标记错误后，异步触发该 source 点的 Agent 重修复

### C6: CALLS 边 location 属性结构化存储
- **问题**: Neo4j 不支持嵌套对象，需决定存储方式
- **决策**: 使用两个独立属性 `call_file: str` + `call_line: int`（而非 JSON 字符串），便于索引和查询

### C7: config.yaml 环境变量插值 `${VAR_NAME}`
- **问题**: 计划未实现变量替换
- **修正**: config 加载时用 `os.path.expandvars()` 处理 YAML 值中的 `${VAR_NAME}` 语法

## High Gaps (影响功能)

### H1: AI4CParser 复用策略
- **决策**: Copy + Adapt（复制核心文件到项目内，适配 LanguagePlugin Protocol）。不用 submodule（避免版本耦合）。
- **适配清单**:
  - `ast_parser.py` → `symbol_extractor.py`（输出 Function dataclass）
  - `call_graph_builder.py` → `call_graph.py`（输出 CallEdge dataclass）
  - `indirect_call_resolver.py` → 整合到 `plugin.py`（输出 UnresolvedCall dataclass）
  - `neo4j.py` → `neo4j_store.py`（适配新 schema）

### H2: codewiki_lite API 响应 schema
- **决策**: 定义 Pydantic 模型映射 API 响应 → SourcePoint 节点。包含 module 层级信息（用于可视化聚合）。

### H3: Neo4j 7 个索引全部作为 1.7 验收标准
- **索引列表**: idx_file_hash, idx_function_file, idx_function_sig, idx_source_kind, idx_calls_resolved, idx_gap_status, idx_gap_caller

### H4: SourcePoint 状态机
- **状态**: pending → running → complete | partial_complete
- **转换时机**: Agent 启动时 set running；门禁通过 set complete；有 unresolvable GAP set partial_complete

### H5: 进度通信 schema
- **progress.json**: `{fixed_gaps: int, total_gaps: int, current_gap_id: str}`
- **Orchestrator 轮询**: 每 2s 读取
- **API 响应**: 聚合所有 source 点进度

> **Superseded (部分) by [ADR 0004](./0004-progress-json-schema-correction.md) — 2026-05-13**
> 实际落地的 canonical schema 为 `{gaps_fixed, gaps_total, current_gap}`（与
> `/api/v1/analyze/status` + 前端 `SourceProgress` interface 对齐）。
> hook 侧双读新旧事件键、单写 canonical schema；详见 ADR 0004。

### H6: LLM 后端 config 驱动命令构造
- **决策**: 从 config 读取 `agent.backend` → 选择对应的 `command + args` → 构造 subprocess 命令

### H7: icsl_tools.py query-reachable 输出 schema
- **格式**: `{nodes: [{id, name, signature, file_path}], edges: [{source, target, resolved_by}], unresolved: [{id, caller_id, call_expression, call_file, call_line, call_type, candidates, source_code_snippet}]}`

### H8: 前端分层聚合（compound nodes）
- **决策**: 使用 Cytoscape.js compound nodes，模块信息来自 codewiki_lite API

### H9: 反例泛化异步触发
- **决策**: 使用 FastAPI BackgroundTasks，审阅 API 标记错误后异步调用 FeedbackStore

## Consequences

- 实施计划需更新，补充上述修正
- Phase 1.1 的 Pydantic Settings 模型需覆盖完整 config 结构
- Phase 2.5 (Orchestrator) 拆分为更细粒度的子任务
- Phase 2.7 (增量更新) 拆分为 5 个子步骤
