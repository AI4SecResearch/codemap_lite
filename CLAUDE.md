# codemap-lite — Call Graph Preprocessor

## Project Overview

codemap-lite 解析 C/C++ 目标代码 → 构建函数级 Call Graph → 存入 Neo4j → 用 subprocess LLM Agent（claudecode / opencode CLI）修复间接调用 → 前端审阅修复结果。不做污点传播审计。上游依赖 `codewiki_lite`（REST API 提供 source 点）。

**核心流程**：两阶段解析（静态 tree-sitter 全量 → 每个 source 点启动一个 CLI Agent 自主 BFS 修复）+ 门禁验证（每个 GAP 最多 3 次重试）+ 反例库反馈（LLM 泛化去重，通过 CLAUDE.md 注入下一轮）。

完整背景见 [`docs/architecture.md`](docs/architecture.md)；端到端验证方案见 [`docs/e2e-test-plan.md`](docs/e2e-test-plan.md)。

---

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (via `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Uses default label vocabulary (needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: one `CONTEXT.md` at repo root + `docs/adr/`. See `docs/agents/domain.md`.

---

## Architecture at a glance

- **6 层**（Parsing/Ingestion → Static Analysis → Graph Storage → Repair Agent → REST API → Frontend），详见 `docs/architecture.md §1`。
- **Neo4j schema**：`File` / `Function` / `SourcePoint` / `UnresolvedCall` / `RepairLog` 五类节点；`DEFINES` / `CALLS` / `HAS_GAP` / `IS_SOURCE` 四类关系。`CALLS.resolved_by ∈ {symbol_table, signature, dataflow, context, llm}`。
- **Repair Agent** 是 subprocess（不是 SDK API），source 间并发（默认 max 5），source 内串行；工作目录 = 目标代码目录；通过 `agent/claude_md_template.py` 在运行时生成目标目录下的 `CLAUDE.md` + `.icslpreprocess/` 注入文件。该运行时 CLAUDE.md 与本文件是两个不同的东西。
- **决策记录**：`docs/architecture.md §附录` 有 56 条 ADR 速查表；详细 gap 分析见 `docs/adr/`。

---

## Repository layout

```
codemap_lite/
├── cli.py                  # Typer 入口（analyze / repair / status / serve）
├── config/
│   ├── settings.py         # Pydantic Settings（6 个顶层 section）
│   └── default_config.yaml
├── parsing/                # Layer 0-1：tree-sitter 解析 + 3 层静态间接调用解析
│   ├── base_plugin.py      # LanguagePlugin Protocol
│   ├── plugin_registry.py
│   └── cpp/                # CppPlugin（复用 AI4CParser）
├── graph/                  # Layer 2：Neo4j 读写 + 增量 + 查询
│   ├── schema.py
│   ├── neo4j_store.py      # 所有 Neo4j 写操作的唯一入口
│   ├── query_engine.py
│   └── incremental.py
├── analysis/               # Layer 3：Repair 编排
│   ├── repair_orchestrator.py
│   ├── prompt_builder.py
│   ├── source_point_client.py
│   └── feedback_store.py
├── agent/                  # Agent 侧注入物
│   ├── icsl_tools.py       # 图查询 + 边写入 + 门禁
│   ├── claude_md_template.py
│   └── hooks/              # PostToolUse / Notification hook 脚本
├── api/                    # Layer 4：FastAPI
│   ├── app.py
│   └── routes/             # graph / review / analyze / feedback / source_points
└── pipeline/
    └── orchestrator.py     # 全流程编排

tests/                      # 16 个 test_*.py + run_e2e_full.py + run_e2e_repair.py
frontend/                   # Layer 5：React 18 + TS + Vite + Cytoscape.js + Tailwind
docs/                       # architecture.md / e2e-test-plan.md / adr/ / agents/
```

---

## Development environment

- **Python**：>=3.10，构建工具 hatchling（`pip install -e .`）
- **运行时依赖**：typer / pydantic(+settings) / pyyaml / neo4j / fastapi / uvicorn / httpx / tree-sitter / openai / jinja2
- **开发依赖**：pytest / pytest-asyncio
- **前端**：React 18 + TypeScript + Vite + Cytoscape.js + Tailwind
  ```bash
  cd frontend && npm install
  npm run dev      # dev server
  npm run build    # tsc + vite build
  npm run preview  # 预览产物
  ```
- **前置服务**：Neo4j 5.x 运行中；`codewiki_lite` REST 可达；修复 E2E 需要 `claudecode` 或 `opencode` CLI

---

## Running the CLI

```bash
codemap-lite analyze  [--config PATH] [--incremental]   # 解析 + 构图
codemap-lite repair   [--config PATH]                   # 只跑修复 Agent
codemap-lite status   [--config PATH]                   # 打印进度
codemap-lite serve    [--config PATH] [--port 8000]     # 启 FastAPI
```

---

## Configuration

`config.yaml` 六个顶层 section：`project` / `neo4j` / `codewiki_lite` / `agent` / `visualization` / `feedback`。Schema 定义见 `codemap_lite/config/settings.py`，默认模板见 `codemap_lite/config/default_config.yaml`。敏感值用 `${VAR_NAME}` 从环境变量注入。

---

## Testing

- **单元 / 集成**：`pytest`，16 个 `test_*.py` 覆盖 cli、config、parsing、graph store、icsl_tools、repair orchestrator、prompt builder、feedback store、hooks、api、pipeline、incremental。
- **E2E 驱动脚本**：
  - `python -m tests.run_e2e_full`  —— 静态分析全链路（CastEngine）
  - `python -m tests.run_e2e_repair --sample N --timeout 240`  —— 真实 opencode + GLM-5 修复
- **E2E 场景**：7 个（解析正确性 / GAP 识别 / Agent 修复 / 增量 / 反例反馈 / 跨模块 / 门禁重试），对应基线见 `docs/e2e-test-plan.md`。
- **opencode + GLM-5 via DashScope** 的凭证与 `--pure` / `--dangerously-skip-permissions` 注意事项见 `docs/e2e-test-plan.md §E2E-3`。

---

## Claude Loop Workflow

### 总体策略：先跑通最小系统，再逐步完善至架构设计

`docs/architecture.md`（13 章 + 56 条 ADR）是本仓库的**唯一事实来源（Single Source of Truth）**。但实现路径不是一步到位——**首要目标是尽快跑通架构中描述的核心功能链路（最小可用系统），然后在此基础上逐轮完善，最终达成与架构设计完全一致**。

**两阶段推进**：
1. **Phase 1 — 最小系统（Make it work）**：优先实现架构中端到端链路必需的功能（解析 → 构图 → 修复 → 审阅），每个环节只做"能跑通"的最简实现。跳过的细节（性能优化、边界校验、高级特性）登记到 `## Known gaps`，不阻塞主链路。
2. **Phase 2 — 逐步对齐（Make it right）**：最小系统跑通后，每轮 loop 从 `## Known gaps` 和架构差距清单中挑差距点，用测试锁定预期行为，实现对齐。持续迭代直到实现与 `docs/architecture.md` 完全一致。

**判断当前处于哪个 Phase**：如果 `codemap-lite analyze → repair → serve` 全链路还不能在真实目标代码上跑通产出可审阅的结果，就还在 Phase 1；否则进入 Phase 2。

### 铁律（两个 Phase 都适用）

- **架构冲突处理顺序**：`docs/architecture.md` > ADR（`docs/adr/`）> 代码 > 测试。发现矛盾时，**先开 ADR 记录并修订架构文档，再改代码/测试**；绝不允许静默修改 `architecture.md` 来"迎合"已经写好的代码。
- **偏离必须显式登记**：任何暂时无法对齐的实现，必须在 `## Known gaps` 或新 ADR 里写清楚（用架构里的原词命名），不允许隐式 gap。
- **术语锁死**：节点 / 关系 / `resolved_by` 枚举 / 配置 section 名 / CLI 命令名 / REST 路径——严格使用 `architecture.md` 中的字面拼写，不改名、不加别名。

### 每轮循环

1. **Propose（识别差距 → 选定本轮目标）** — 先查 `gh issue list`（处理已有 open issue 优先于开新坑）。对照 `architecture.md` 相关章节，列出差距清单。**Phase 1 优先选阻塞端到端链路的缺失功能**；Phase 2 优先选能提升正确性/健壮性的差距点。一轮挑 1–3 个目标。**issue 描述必须引用 `architecture.md` 的具体章节/ADR 编号**。
2. **Implement（测试先行 → 实现）** — 对本轮选定的每个目标：① 先写测试用例，断言架构文档描述的预期行为（RED）；② 实现/修改代码使测试通过（GREEN）；③ 重构清理（REFACTOR）。Phase 1 允许测试只覆盖 happy path；Phase 2 补齐边界和异常路径。棘手 bug 走 `/diagnose`。**不引入架构未定义的新概念**；确有必要，走步骤 5 先写 ADR 再回来实现。
3. **Verify（端到端，必须全过）** —
   - **架构对齐**：diff 当前实现 vs `docs/architecture.md` 相关章节，逐条确认本轮改动与架构一致。
   - **后端**：`pytest` + 相关 `tests/run_e2e_*.py`。改动触及 `docs/e2e-test-plan.md §E2E-1..7` 中尚未覆盖的场景时，**新增测试用例**而不是跳过。
   - **前端**：`cd frontend && npm run dev` 人眼过一遍——检查项见 `### 前端持续优化`。
   - **契约**：改 REST API 时同步 `docs/architecture.md §8`、前端 API 客户端、相关 E2E 场景——三处同步更新才算完成。
4. **Commit & Push** — `/commit`（或直接 `gh`），遵循 conventional commits。**commit body 必须引用 `architecture.md` 章节或 ADR 编号**，说明本次对齐了哪个 gap。具体 remote / 认证 / 分支策略见 `### Git Conventions`。
5. **Learn** — 本轮新事实按归属分流：架构级决策走 ADR；新增工程约定写进 `## Project-specific conventions`；暂时无法对齐的实现写进 `## Known gaps`；本轮做了什么交给 `git log`。改完后用 `/simplify` 做一次自审。
6. **Repeat** — 选下一个最高价值 issue。Phase 1 优先打通链路；Phase 2 优先缩小架构 gap。每轮只闭环 1–3 个点，积少成多，最终实现与 `docs/architecture.md` 完全一致。

### Git Conventions（本仓库具体）

- **Remote**：`origin = https://github.com/AI4SecResearch/codemap_lite.git`（HTTPS），`main` 已追踪 `origin/main`。认证走系统 `credential.helper=store`（`~/.git-credentials` 已存 github.com token），`gh` CLI 复用同一套凭证——直接 `git push` / `gh pr create` 即可。禁止重跑 `gh auth login`、重配 `user.name` / `user.email`、生成新 PAT。
- **分支策略**：单人开发，直接在 `main` 工作并 `git push origin main`；只有确需并行多条改动时才开 feature 分支并通过 PR 合回。
- **Commit 规则**：prefix 白名单 `feat:` / `fix:` / `refactor:` / `test:` / `docs:` / `chore:` / `perf:` / `ci:`（全局见 `~/.claude/rules/git-workflow.md`）。**One feature per commit**；body 引用 `architecture.md` 章节或 ADR 编号；never skip tests（每 feature 至少一个测试，断言对标架构行为）；绝不 `--no-verify` 跳钩子；**push only after green = 本地 `pytest` + `npm run build` 全绿才 push**（仓库暂无 `.github/workflows/` CI，绿由本地跑出；接入 CI 后改为以 CI 结果为准）。
- **Agent coordination（子 agent 协作安全）** —
  - subagent 开工前先 `git status` 确认 clean（必要时 `git stash` 暂存主线改动）；subagent 结束后先 `git diff` 审视改动，`pytest` + `npm run build` 全绿再 commit。
  - 若 subagent 把 build 搞坏，**优先 `git checkout HEAD -- <path>` 回滚对应文件**再手动重做，不要在坏基础上叠加。
  - subagent 偏好用于**新建文件**；跨 feature 共享的核心文件（`codemap_lite/cli.py` / `codemap_lite/graph/neo4j_store.py` / `codemap_lite/analysis/repair_orchestrator.py` / `frontend/src/App.tsx` / `frontend/src/main.tsx`）优先主会话手动改。
  - 后台 / 并行 subagent 要在规划时就拆开文件集合，避免两个 agent 同时改同一文件。

### 前端持续优化（Claude 自驱）

前端不是"架构文档的附属"——它的**唯一目标是让人眼审阅 repair 结果更快更准**。Claude 主动发起前端优化 issue、不必等用户提需求。

**每轮 Verify 必跑的人眼检查清单**（`npm run dev` 后过一遍）：
- Cytoscape 分层聚合是否正确（模块 → 文件 → 函数），大图是否卡顿
- 审阅面板 review 交互（标记正确 / 错误 / 反例）是否可用
- 顶部进度指示（已修复 / 待修复 / 失败 GAP）是否准确

**北极星指标**（每轮至少一项改善）：
- **单个 GAP 的审阅耗时**：从打开审阅面板到做出"正确/错误/反例"决策，能否更少点击、更少滚动？
- **调用链可信度可见性**：`resolved_by` 五档（symbol_table / signature / dataflow / context / llm）是否有清晰的视觉区分（颜色/图标/边样式）？llm 修复的边是否一眼可辨？
- **大图可导航性**：CastEngine 级别（数千函数）下 Cytoscape 是否不卡；三级聚合展开是否顺畅。
- **状态透明度**：修复进度、retry 次数、门禁通过/失败、反例命中——是否都在 UI 上可见，而不是只在后端日志。
- **反例工作流**：审阅者标记反例后，是否能当场看到反例库去重结果、泛化摘要，并知道下一轮会被注入到 **repair 运行时 CLAUDE.md**（由 `agent/claude_md_template.py` 生成，见 Project-specific conventions "两个 CLAUDE.md 别混"）的哪一段。

**候选优化方向**（按价值排序，每轮挑一项完成闭环）：
1. 审阅面板信息密度：调用上下文代码片段、相邻 GAP 跳转、键盘快捷键（`y`/`n`/`c` 对应正确/错误/反例）
2. 调用图视觉语言：按 `resolved_by` 上色；llm 修复的边加虚线 + 小 ★；未解决 GAP 红色醒目标记
3. 分层布局与聚合：默认按文件/模块折叠；hover/点击展开；大图用 fcose/cola 布局 + 视口级懒加载
4. 进度与可观测性：顶部全局进度条；每个 source 点的迷你状态卡
5. 反例可视化：专门的反例库视图，显示原始反例 → LLM 泛化后的规则 → 被哪些 repair 轮次引用
6. 对比视图：修复前后 diff（新增的 CALLS 边、消失的 GAP）
7. 空状态 / 错误态 / 加载态：骨架屏、友好报错、重试按钮
8. 可访问性与移动端：暗色模式、键盘导航、至少平板可用

**工作方式**：
- 每轮 `npm run dev` 跑完，至少提出 1 条"本轮发现的前端可改进点"作为下一轮候选 issue（即使后端仍是主线）
- 优化 issue 的标题用 `frontend:` 前缀，body 说明对应哪项北极星指标 + 预期改善
- UI 改动涉及新字段时，先同步到 `docs/architecture.md §8`（REST schema）再改前端，遵守 architecture-first
- 纯样式/交互优化不需要写 ADR，但 commit body 仍需说明对齐了哪个北极星指标 + issue 标题 `frontend:` 前缀

---

## Project-specific conventions

- **新 CLI 子命令** 都加到 `codemap_lite/cli.py`（Typer app），不要另起入口。
- **Neo4j 写操作** 只走 `graph/neo4j_store.py`，业务层不直接持有 driver。
- **两个 CLAUDE.md 别混**：本文件给在本仓库工作的 Claude session 用；`agent/claude_md_template.py` 在每次 repair 时在**目标代码目录**生成另一份 CLAUDE.md，给 repair-agent subprocess 用，完成后清理。
- **`resolved_by` 取值** 严格限定在 `symbol_table / signature / dataflow / context / llm`，新增需同步 schema + 前端过滤器。
- **Source 点** 从 `codewiki_lite` REST 拿，别在本仓库硬编码；mock 场景用 `tests/` 下的 fixture。
- **Git 操作** 见 `### Git Conventions`（remote / 认证 / 分支 / commit 规则 / subagent 协作都在那里）。

---

## Known gaps

- ~~`codemap_lite/agent/icsl_tools.py` 架构里描述为 CLI（`icsl_tools.py query-reachable / write-edge / check-complete`），当前实现是 **3 个 Python 函数** + `GraphStoreProtocol`。Agent subprocess 侧的 CLI 封装尚未落地。~~ **已完成（2026-05-12）**：`icsl_tools.py` 增加 argparse 入口 + `__main__` 守卫，三个子命令直通 in-process 函数；`repair_orchestrator._inject_files` 将文件复制到 `.icslpreprocess/icsl_tools.py`；新增 6 个 CLI 测试；对齐 architecture.md §3 Repair Agent 工具协议。
- ~~`Neo4jGraphStore` 17 个方法全是 `NotImplementedError` stub；`_check_gate` 直接 `return True`；`cli.py repair` 不向 `RepairConfig` 传 `graph_store`——三处 prod blocker 串成"产线发布"路径。~~ **已完成（2026-05-13）**：(a) `Neo4jGraphStore` 17 个方法全部用 neo4j 驱动 Cypher 实现（`MERGE` 节点；CALLS 边按 `(call_file, call_line)` 唯一；`get_pending_gaps_for_source` 走 `[:CALLS*0..]` BFS）+ lazy driver；(b) `_check_gate` subprocess 调 `python .icslpreprocess/icsl_tools.py check-complete --source <id>` 解析 JSON `complete` 字段，spawn/exit/JSON 任一异常一律 `False`；(c) `cli.py` 新增 `_build_graph_store(settings)` 工厂并把它穿进 `RepairConfig.graph_store`，retry 审计字段（`last_attempt_timestamp` / `last_attempt_reason`）正式落到 Neo4j。新增 16 个测试覆盖；176 tests + `npm run build` 全绿。对齐 architecture.md §3 门禁机制 + Retry 审计字段 + §4 Cypher 契约。
- ~~`check-complete` CLI 对 `Neo4jGraphStore` / `InMemoryGraphStore` 必抛 `TypeError: 'UnresolvedCallNode' object is not subscriptable`——协议类型签名声明 `list[dict[str, Any]]` 但真 store 返回 `list[UnresolvedCallNode]` dataclass，每次 `check-complete` stdout 都是 `{"error":"TypeError",...}`，orchestrator `json.loads().get("complete")` 拿不到字段，门禁**永远 False**。mock store 返回 dict 掩盖了问题。~~ **已修复（2026-05-13，7-entry E2E 跑完暴露）**：新增 `_gap_id()` helper 对 dict / dataclass 两种形态都取 `id`；协议签名放宽为 `list[Any]` 并注明兼容约定；新增 regression test `test_check_complete_accepts_dataclass_pending_gaps`。177 tests + `npm run build` 全绿；live Neo4j 验证 `check-complete --source 9992de1ac1aa` 正确返回 `{"complete": false, "remaining_gaps": 3, "pending_gap_ids": [...]}`。对齐 architecture.md §3 门禁机制返回契约。
- 仓库无 `pytest.ini` / `pyproject.toml [tool.pytest]`，跑测试依赖默认发现。
- 无 `black` / `ruff` / `mypy` 配置文件——依赖全局 `~/.claude/rules/coding-style.md` 的默认值。
- `codemap-lite repair` / `status` / `serve` 已从 stub 升级为真实编排（2026-05-12）：`repair` 读 config → `SourcePointClient.load_from_file` 或 `.fetch()` → `RepairOrchestrator.run_repairs` 并发修复并汇总成功/失败；`status` 读 `.icslpreprocess/state.json` + `logs/repair/*/progress.json`；`serve` 真实 `uvicorn.run(create_app(), host, port)`。`analyze` 增量分支保持原状。
- `docs/agents/domain.md` 规定仓库根目录应有一个 `CONTEXT.md`，目前不存在——本 CLAUDE.md 暂时兼任 root context；待 domain docs 成熟再单独拆出。
- 仓库暂无 `.github/workflows/` CI，Loop Workflow 里的"push only after green"当前以**本地** `pytest` + `npm run build` 为准。
- **`docs/test-plan-opencode-glm-castengine.md` 未落盘**（2026-05-13）：7-entry E2E 已跑出实测遥测（34 RepairLog、35 LLM 边、100% reasoning_summary 非空、GLM-5 在 3/7 source 上 0-edge 放弃的模式），但 opencode + GLM-5 + CastEngine 的全量测试计划文档尚未成文。应覆盖的断言：reasoning 捕获命中率、放弃率按 source kind 分布、mirror 吞吐、门禁通过率按 source kind 分布。
- **35 LLM 边 vs 34 RepairLog 的 1 条差值**（2026-05-13，7-entry run 观察到）：`65cecfa5cf3a→f41875af7db5 @line132` 与 `94a77ea67585→1ccd8bba9eab @line1189` 两条 CALLS 边在 Neo4j 里没有 `(caller_id, callee_id, call_location)` 三元组精确匹配的 RepairLog。怀疑 `create_repair_log` 的 `MERGE` 键与 `write_edge` 时拼出的 `call_location` 格式不一致，或 RepairLog 的 `MERGE` 键太粗导致同一 agent 调用内第二次 `write-edge` 覆盖了前一条。未深究、未定位根因。
- **门禁机制尚未真 pass 过**（2026-05-13）：`check-complete` TypeError 修复前每次必 False；修复后的 7-entry run 所有 source 都有剩余 pending GAP（0-edge 放弃 + counter-example 全覆盖），没跑出过 `complete=True` 的实际路径。下一次 E2E 会是第一次真门禁验证——需要挑一个 agent 能真正解全 GAP 的 source 或构造用例。
- 仓库根目录没有 check-in 的 `config.yaml`，4 个 CLI 子命令默认 `--config config.yaml` 找当前工作目录——首次跑前要么 `cp codemap_lite/config/default_config.yaml config.yaml`，要么显式 `--config codemap_lite/config/default_config.yaml`。
- **FeedbackStore 反例泛化去重（architecture.md §3 反馈机制 step 4 "相似 → 总结合并"）** 当前实现是**精确 pattern 字串匹配**（`FeedbackStore.add()` 按 `pattern` 精确比对决定是否 dedup），而架构要求**LLM 语义相似度判断**后合并+泛化。当前行为：相同 pattern 的第二次提交会被标记为 `deduplicated=True` 但泛化摘要（如"所有 dispatcher vtable resolution 必须匹配 signature"）不会自动生成——只在 pattern 文本完全一致时才"合并"。缺失的部分：调用 LLM 判断两个反例是否语义相似；相似时生成泛化摘要替换原始 pattern；pattern 中包含具体行号的反例应自动去掉行号泛化为模式级规则。此 gap 影响反例库的实际威力——精确匹配无法捕获"同一 bug 模式在不同位置"的情况。
- ~~REST API 7 个列表端点返回裸列表而非 `{total, items}` 分页格式。~~ **已完成（2026-05-14）**：`/files`, `/functions`, `/functions/{id}/callers`, `/functions/{id}/callees`, `/source-points`, `/reviews`, `/feedback` 全部改为 `{total, items}` + `limit/offset` 参数，对齐 architecture.md §8 REST API 契约。492 tests 全绿。
- ~~**前端视觉系统缺失**（ADR-0007）：无共享组件库、无图标系统、无骨架屏、无确认对话框、无键盘快捷键、无搜索过滤。~~ **已完成（2026-05-16）**：引入 lucide-react + `components/ui/` 组件库（Button/Badge/Card/ProgressBar/SearchInput/Skeleton/ConfirmDialog/EmptyState/Timestamp）+ Inter 字体 + 全局视觉规范 + SourcePointList 批量选择/键盘快捷键 + Dashboard 修复效果趋势 + FeedbackLog 美化。对齐 ADR-0007。
- ~~**前端操作动线 GAP（ADR-0008，2026-05-16）**：从用户操作动线审视，以下功能缺失：~~
  ~~- **P0 Agent 实时日志不可见**~~
  ~~- **P0 修复触发无即时反馈**~~
  ~~- **P0 SourcePointList ↔ RepairActivity 无联动**~~
  ~~- **P1 Repair All 无确认对话框**~~
  ~~- **P1 修复结果默认折叠**~~
  ~~- **P1 失败 GAP 无重试入口**~~
  ~~- **P1 日志无按 source 过滤**~~
  ~~- **P2 状态含义不明**~~
  ~~- **P2 失败分类摘要缺失**~~
  ~~- **P2 反例库无 CRUD**~~
  ~~- **P2 模块分组视图缺失**~~
  ~~- **P2 历史趋势缺失**~~
  **已完成（2026-05-17）**：全部 12 项前端 GAP 修复落地。后端：`GET /api/v1/repair-logs/live`（tail API）+ `DELETE/PUT /api/v1/feedback/{id}`（CRUD）+ `log_dir` 默认值修复。前端：`AgentTerminal` 组件（2s 轮询 + 终端框 + 完成链接）、`handleRepairSource` 即时反馈（不 reset repairing 直到 finished）、RepairActivity `?source=` 过滤 + 下拉、SourcePointList → RepairActivity 链接、Repair All 确认对话框、修复结果/GAP 默认展开、"重试此 Source" 按钮、状态 Badge tooltip、失败分类摘要、FeedbackLog 搜索+编辑+删除、模块分组视图切换、Dashboard 历史趋势。新增 11 个测试（`TestRepairLogsLiveEndpoint` 5 个 + `TestFeedbackDeleteUpdate` 6 个）。226 tests + `npm run build` 全绿。对齐 ADR-0008。
- **测试覆盖 GAP（2026-05-17 gap-analysis round 4）**：
  - ~~Live tail endpoint 无测试~~ **已修复**：`TestRepairLogsLiveEndpoint`（5 tests）覆盖空目录、tail 读取、finished 状态、多 attempt 选最新、必填参数校验。
  - ~~Feedback DELETE/PUT 无测试~~ **已修复**：`TestFeedbackDeleteUpdate`（6 tests）覆盖删除成功/404、更新成功/404/空 body 422、删除后索引偏移。
  - **SourcePointList ↔ RepairActivity 端到端集成测试缺失**：前端组件间联动（URL 参数传递、轮询状态同步）需要 E2E 浏览器测试或 Playwright 覆盖，当前仅有 API 层单元测试。
- **架构对齐验证（2026-05-17 gap-analysis round 4）**：
  - §3 Repair Agent：5 类失败审计（`subprocess_crash` / `subprocess_timeout` / `agent_error` / `agent_exited_without_edge` / `gate_failed`）全部在 `repair_orchestrator.py` 实现（lines 418/448/472/509/359）。
  - §4 Neo4j Schema：`HAS_GAP` 关系在 `neo4j_store.py:901` 创建；`IS_SOURCE` 关系在 `neo4j_store.py:1556` 创建。**已对齐**。
  - §8 REST API：28 个前端 API 方法全部有对应后端端点，响应格式一致。**已对齐**。
  - §3 门禁 subprocess 调用：`_check_gate` 正确 spawn `python .icslpreprocess/icsl_tools.py check-complete --source <id>` 并解析 JSON。**已对齐**。
  - §3 超时处理：`asyncio.wait_for` + `proc.kill()` + stamp `subprocess_timeout: <N>s`。**已对齐**。
- ~~**库函数/系统调用被误标为 UnresolvedCall（2026-05-17）**：tree-sitter 解析层对 `c_str()`, `CLOGD()`, `CLOGE()`, `promote()`, `Message()`, `HILOGI()` 等标准库函数/日志宏/智能指针方法产生了 UnresolvedCall 节点。CastEngine 实测中 395 个 GAP 里约 118 个（30%）是此类误报。~~ **已完成（2026-05-17）**：新增 `codemap_lite/parsing/cpp/library_whitelist.py`（frozenset 100+ 条目，覆盖 STL 方法/智能指针/OHOS 日志宏/基础设施宏/C stdlib/同步原语/运算符与 cast），`is_library_call()` 支持全名和 `::` 后缀匹配。过滤集成于 `call_graph.py:build_calls()` 返回前 + `orchestrator.py` 两处 UC 创建点。CastEngine 实测 edge+UC 冲突从 ~1026 降至 ~718（~30% 减少）。10 个测试覆盖。对齐 architecture.md §1 Parsing 层白名单。
- gap-analysis 历史：`docs/adr/0001-gap-analysis-corrections.md` / `0002-gap-analysis-round2.md` / `0003-gap-analysis-round3.md`。

---

## Key references

- Architecture：[`docs/architecture.md`](docs/architecture.md) — 13 章 + 56 条 ADR
- E2E Plan：[`docs/e2e-test-plan.md`](docs/e2e-test-plan.md) — 7 场景 + CastEngine 基线
- Gap analysis ADRs：[`docs/adr/0001-gap-analysis-corrections.md`](docs/adr/0001-gap-analysis-corrections.md) · [`0002-gap-analysis-round2.md`](docs/adr/0002-gap-analysis-round2.md) · [`0003-gap-analysis-round3.md`](docs/adr/0003-gap-analysis-round3.md)
- Agent skills docs：[`docs/agents/`](docs/agents/) — issue-tracker / triage-labels / domain
- 复用的上游仓：AI4CParser（解析）· codewiki_lite（source 点 REST）· codemap（Neo4j 存储层原型）
