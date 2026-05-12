# ADR-0003: 第三轮 GAP 分析 — 微观层面残余

## Status: Accepted

## Context

第三轮逐行对比，聚焦于之前两轮可能遗漏的实现细节和边界条件。

---

## 残余 GAP

### G-R1: 注入文件中 `config.yaml` 只含 Neo4j 连接信息

- **架构**: Section 3 line 159 — `.icslpreprocess/config.yaml` 注释为 "Neo4j 连接配置（icsl_tools.py 读取）"
- **计划**: 2.5 提到 "注入文件生成" 但未明确这个 config.yaml 是主 config 的子集（仅 neo4j section）还是完整复制
- **修正**: 注入的 config.yaml 只包含 `neo4j:` section（uri, user, password），不暴露其他配置给 Agent

### G-R2: JSONL 日志按 GAP 分文件

- **架构**: Section 3 line 194 — "logs/repair/{source_id}/**{gap_id}**.jsonl"（每个 GAP 一个 JSONL 文件）
- **计划**: 2.4 只说 "JSONL 格式"，未明确是每个 GAP 一个文件还是每个 source 一个文件
- **修正**: 每个 GAP 独立一个 JSONL 文件（`{gap_id}.jsonl`），便于后续按 GAP 提取 reasoning_summary

### G-R3: 门禁中 retry_count 递增的精确时机

- **架构**: Section 3 line 115 — "有残留 → 残留 GAP 的 retry_count++"
- **计划**: 2.5 说 "重试逻辑（GAP 级别，max 3）" 但未明确: 是门禁失败时对**所有**残留 GAP 递增 retry_count，还是只对本次 Agent 尝试过但未修复的 GAP 递增
- **修正**: 门禁失败时，对该 source 可达路径上所有 `status=pending` 的 UnresolvedCall 递增 retry_count（因为 Agent 有机会修复它们但没有）

### G-R4: Agent 冲突处理 — "检查边是否已存在，已存在则跳过"

- **架构**: Section 3 line 101 — "Agent 修复前检查边是否已存在，已存在则跳过"
- **计划**: 2.5 提到 "乐观锁" 在风险表中，但这个行为实际是 Agent 内部的（在 CLAUDE.md prompt 中指示），不是 Orchestrator 的
- **修正**: 这个逻辑需要在两个地方实现:
  1. **icsl_tools.py write-edge**: 写入前查询边是否已存在，已存在则返回 "skipped"（而非报错）
  2. **CLAUDE.md prompt**: 指示 Agent 在 write-edge 返回 skipped 时继续处理下一个 GAP

### G-R5: `icsl_tools.py` 作为独立可执行文件的运行环境

- **架构**: Section 3 line 169 — "复制 icsl_tools.py 到 .icslpreprocess/"
- **计划**: 2.2 定义了 icsl_tools.py 的功能，但未考虑它被复制到目标目录后的依赖问题
- **修正**: icsl_tools.py 必须是**自包含**的（或附带 requirements）:
  - 依赖: neo4j driver, pyyaml, typer/click
  - 选项 A: 单文件脚本，内联所有逻辑，只依赖 neo4j + pyyaml（pip install 到系统）
  - 选项 B: 复制整个 .icslpreprocess/ 目录作为 mini package
  - **决策**: 选项 A — 单文件脚本，假设目标环境已有 neo4j driver（作为前置条件文档化）

### G-R6: `ProgressTracker` 作为独立组件

- **架构**: Layer 3 diagram line 33 — "ProgressTracker (内存)" 作为独立组件列出
- **计划**: 2.5 提到 "进度追踪（内存字典，每 2s 轮询 progress.json）" 但未将其作为独立类
- **修正**: ProgressTracker 作为 2.5 的内部类即可（不需要独立文件），但需要定义清晰的接口供 API 层查询:
  - `get_status() -> dict` — 返回所有 source 点的进度汇总
  - `get_source_status(source_id) -> dict` — 返回单个 source 点的详细进度

### G-R7: 增量更新步骤 3 的精确语义 — "指向变更函数" vs "从变更函数出发"

- **架构**: Section 7 line 407 — "变更函数的 **callers** 中如有 LLM 修复的边**指向旧函数**"
- **计划**: 2.7 步骤 3 说 "查找 resolved_by=llm 且指向变更函数的 CALLS 边"
- **差异**: 架构说的是 "callers 中有 LLM 边指向旧函数"，即方向是 `caller -[CALLS {resolved_by:llm}]-> changed_function`。计划的表述一致，但需要确认: 是否也要失效 `changed_function -[CALLS {resolved_by:llm}]-> any_callee`（从变更函数出发的 LLM 边）？
- **修正**: 两个方向都要处理:
  - 入边: `any_caller -[CALLS {resolved_by:llm}]-> changed_function` → 删除（因为 changed_function 可能已被重命名/删除）
  - 出边: `changed_function -[CALLS {resolved_by:llm}]-> any_callee` → 删除（因为 changed_function 的调用逻辑可能已变）
  - 注: 步骤 2 已经删除了 changed_function 的所有关联边并重解析，所以出边已被处理。步骤 3 只需处理入边（其他函数通过 LLM 修复指向 changed_function 的边）

### G-R8: `feedback` config section 未在架构文档 Section 10 中定义

- **架构**: Section 10 config.yaml 只有 project, neo4j, codewiki_lite, agent, visualization 五个 section
- **计划**: 2.6 新增了 `feedback` section（model, base_url, api_key）
- **差异**: 这是实施计划对架构的**扩展**（ADR-0002 G14 的决策产物），不是 GAP
- **修正**: 无需修正，但需要在 1.1 的 Pydantic Settings 中预留 `feedback` section（可选字段，有默认值）

---

## 结论

经过三轮对比，**实施计划与架构文档之间不再存在 Critical 或 High 级别的 GAP**。

残余 GAP 均为 Medium/Low 级别的实现细节，可在编码过程中自然解决:
- G-R1 ~ G-R3: 注入文件和日志的精确格式（编码时确定）
- G-R4 ~ G-R5: icsl_tools.py 的边界行为（TDD 时覆盖）
- G-R6: ProgressTracker 组织方式（重构时处理）
- G-R7: 增量更新方向性（已在分析中澄清）
- G-R8: config 扩展（已确认非 GAP）

**建议**: 不再继续 GAP 分析，开始 TDD 编码。
