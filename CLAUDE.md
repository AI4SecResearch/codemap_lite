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

### 最高铁律：实现必须与 `docs/architecture.md` 吻合

`docs/architecture.md`（13 章 + 56 条 ADR）是本仓库的**唯一事实来源（Single Source of Truth）**。任何代码、配置、测试、前端行为都必须与该文档一致。

- **每轮循环开始前**：重新扫读 `docs/architecture.md` 受影响的章节（至少 §1 层次、§2 Neo4j Schema、§3 Repair Agent 协议、§8 REST API 契约），列出"当前实现 vs 架构"的 diff。没有这一步不准进 Implement。
- **每轮循环结束前**：对照同样章节自检，确认本轮没有引入新的漂移。有漂移就必须在提交前解决——要么改代码回到架构，要么开 ADR 正式修订架构。
- **架构冲突处理顺序**：`docs/architecture.md` > ADR（`docs/adr/`）> 代码 > 测试。发现矛盾时，**先开 ADR 记录并修订架构文档，再改代码/测试**；绝不允许静默修改 `architecture.md` 来"迎合"已经写好的代码。
- **偏离必须显式登记**：任何暂时无法对齐的实现，必须在 `## Known gaps` 或新 ADR 里写清楚（用架构里的原词命名），不允许隐式 gap。
- **术语锁死**：节点 / 关系 / `resolved_by` 枚举 / 配置 section 名 / CLI 命令名 / REST 路径——严格使用 `architecture.md` 中的字面拼写，不改名、不加别名。

### 6 步循环（每步都服务于"向架构靠拢"这一目标）

1. **Propose** — 先查 `gh issue list`（处理已有 open issue 优先于开新坑）。如需新方案，用 `/plan` skill 或 planner agent 起草，**issue 描述必须引用 `architecture.md` 的具体章节/ADR 编号**，说明本次改动是在缩小哪个 gap，登记为 issue。
2. **Implement** — 标准顺序：`/plan`（跨模块、新增概念、或含复杂设计时起草方案）→ `/tdd`（先写测试再实现，测试断言直接对标架构文档行为）→ 简单单点修复可跳过 `/plan`。棘手 bug 走 `/diagnose`。**不引入架构未定义的新概念**；确有必要，走步骤 5 先写 ADR 再回来实现。
3. **Verify（端到端，必须全过）** —
   - **架构对齐**：diff 当前实现 vs `docs/architecture.md` 相关章节，逐条确认字段名、接口签名、并发模型、门禁次数、retry 上限等都一致。
   - **后端**：`pytest` + 相关 `tests/run_e2e_*.py`。改动触及 `docs/e2e-test-plan.md §E2E-1..7` 中尚未覆盖的场景时，**新增测试用例**而不是跳过。
   - **前端**：`cd frontend && npm run dev` 人眼过一遍——检查项（人眼清单）、北极星指标、候选优化方向统一在 `### 前端持续优化`。
   - **契约**：改 REST API 时同步 `docs/architecture.md §8`、前端 API 客户端、相关 E2E 场景——三处同步更新才算完成。
4. **Commit & Push** — `/commit`（或直接 `gh`），遵循 conventional commits。**commit body 必须引用 `architecture.md` 章节或 ADR 编号**，说明本次对齐了哪个 gap。具体 remote / 认证 / 分支策略见 `### Git Conventions`。
5. **Learn** — 本轮新约定追加到 `## Loop Iteration Notes`（含"对齐了 §X.Y"或"新增 ADR 000N"）；架构级决策走 `/doc-coauthoring` 写 ADR 并同步回 `architecture.md` 附录速查表；术语漂移走 `/grill-with-docs` 收敛到本 CLAUDE.md 或 `docs/architecture.md`。改完后用 `/simplify` 做一次自审。
6. **Repeat** — 选下一个最高价值 issue，优先挑能进一步缩小架构 gap 的。

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
- 纯样式/交互优化不需要写 ADR，但需要在 `## Loop Iteration Notes` 记一行"frontend: XXX"

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
- 仓库无 `pytest.ini` / `pyproject.toml [tool.pytest]`，跑测试依赖默认发现。
- 无 `black` / `ruff` / `mypy` 配置文件——依赖全局 `~/.claude/rules/coding-style.md` 的默认值。
- `codemap-lite repair` / `status` / `serve` 已从 stub 升级为真实编排（2026-05-12）：`repair` 读 config → `SourcePointClient.load_from_file` 或 `.fetch()` → `RepairOrchestrator.run_repairs` 并发修复并汇总成功/失败；`status` 读 `.icslpreprocess/state.json` + `logs/repair/*/progress.json`；`serve` 真实 `uvicorn.run(create_app(), host, port)`。`analyze` 增量分支保持原状。
- `docs/agents/domain.md` 规定仓库根目录应有一个 `CONTEXT.md`，目前不存在——本 CLAUDE.md 暂时兼任 root context；待 domain docs 成熟再单独拆出。
- 仓库暂无 `.github/workflows/` CI，Loop Workflow 里的"push only after green"当前以**本地** `pytest` + `npm run build` 为准。
- gap-analysis 历史：`docs/adr/0001-gap-analysis-corrections.md` / `0002-gap-analysis-round2.md` / `0003-gap-analysis-round3.md`。

---

## Loop Iteration Notes

- 2026-05-12 · initial-landing: codemap_lite 六层 + frontend + tests + docs/ADR 首次落地到 origin/main（11 个分层 commit），对齐 architecture.md §1-§10；pytest 108/108 绿，`npm run build` 绿。
- 2026-05-12 · frontend: CallGraphView 加 resolved_by 五档视觉语言（teal/green/blue/purple/orange）+ llm 虚线 + ★ 标记 + 左下角图例；对齐 architecture.md §2 CALLS.resolved_by 枚举，兑现北极星指标 #2（调用链可信度可见性）。
- 2026-05-12 · agent: `icsl_tools.py` 补齐 argparse CLI（`query-reachable` / `write-edge` / `check-complete`）+ `__main__`；`repair_orchestrator._inject_files` 把文件复制到 `.icslpreprocess/icsl_tools.py`；closes Known gap #1，对齐 architecture.md §3 Repair Agent 工具协议；pytest 114/114 绿，`npm run build` 绿。
- 2026-05-12 · cli: `codemap_lite/cli.py` 的 `repair` / `status` / `serve` 从 stub 升级为真实编排——`repair` 接入 `SourcePointClient` + `RepairOrchestrator`（并发 + 汇总 + `--source-points-file` 离线模式 + `--log-dir`），`status` 读 `.icslpreprocess/state.json` + `logs/repair/*/progress.json`，`serve` 真实 `uvicorn.run(create_app(), host, port)`；新增 7 个 CliRunner 测试；closes Known gap #4，对齐 architecture.md §9 (ADR #50)；pytest 121/121 绿，`npm run build` 绿。
- 2026-05-13 · frontend: ReviewQueue 加键盘导航——`j/↓` 下一条、`k/↑` 上一条、`y` Mark correct、`n` Mark wrong、`Esc` 清除选择；所选行 ring + 蓝底高亮、自动 `scrollIntoView`；快捷键提示条 + 过滤框/textarea 内自动禁用；兑现候选优化方向 #1（键盘快捷键）+ 北极星指标 #1（单 GAP 审阅耗时）；pytest 121/121 绿，`npm run build` 绿。
- 2026-05-13 · frontend: ReviewQueue 选中行下方内联渲染 `<GapDetail>`——`source_code_snippet` 以 `<pre>` 代码块显示，`var_name` / `var_type` / `retry_count` / `status` 做成彩色 chip（retries>0 琥珀色、status=resolved 绿 / failed 红），候选列表展开到 20 条；快捷键提示条新增"selected row expands with call context"注释；继续兑现候选优化方向 #1（审阅面板信息密度—调用上下文代码片段）+ 北极星指标 #1（单 GAP 审阅耗时）；pytest 121/121 绿，`npm run build` 绿。
- 2026-05-13 · api+frontend: `/api/v1/analyze/status` 从 stub 升级为真实聚合——新增 `_read_source_progress` 读 `<target>/logs/repair/*/progress.json`，返回 `sources: [{source_id, gaps_fixed, gaps_total, current_gap}]` 并据此回填整体 `progress`；`create_app(target_dir=...)` 把配置里的 `project.target_dir` 注到 `app.state`，`cli serve` 同步传入；Dashboard 新增 Repair Progress 面板，每个 source 一张带迷你进度条的小卡片，区分 done/current/idle；对齐 architecture.md §3（Repair Agent 进度文件契约）+ ADR #52，兑现候选优化方向 #4（进度与可观测性）+ 北极星指标 #5（状态透明度）；pytest 123/123 绿（新增 2 个 analyze/status 聚合测试），`npm run build` 绿。
- 2026-05-13 · api+frontend: `/api/v1/feedback` 从 stub 升级为真实读取——`create_app(feedback_store=...)` 把 `FeedbackStore` 注到 `app.state.feedback_store`，route 通过 `dataclasses.asdict` 序列化 `CounterExample`；`cli serve` 实例化 `FeedbackStore(<target>/.codemap_lite/feedback)`（持久化，与 transient `.icslpreprocess/` 分离）；前端 `FeedbackLog` 改为结构化卡片（pattern 标题 + call_context/wrong_target/correct_target 三行带色彩的 code chip）替代原来的 JSON dump；`api/client.ts` 新增 `CounterExample` 类型；对齐 architecture.md §3 反馈机制 + §8，兑现候选优化方向 #5（反例可视化）；pytest 124/124 绿（新增 `test_get_feedback_with_store`），`npm run build` 绿。
- 2026-05-13 · repair: FeedbackStore 反馈回路接通——`RepairConfig` 新增 `feedback_store` 可选字段；`_run_single_repair` 每次重试前调用 `feedback_store.render_markdown()` 把最新反例库写入 `<target>/.icslpreprocess/counter_examples.md`（之前硬编码空串）；`FeedbackStore` 抽出公共 `render_markdown()`，`_write_md` 复用；`cli repair` 与 `cli serve` 共享同一个 `FeedbackStore(<target>/.codemap_lite/feedback)`，API 收到的反例下一轮 repair 即可生效；对齐 architecture.md §3 反馈机制 step 4（"更新 counter_examples.md 最新反例库"）；pytest 126/126 绿（新增 2 个 orchestrator 注入测试），`npm run build` 绿。
- 2026-05-13 · api+frontend: UI→FeedbackStore 写回路打通——`POST /api/v1/feedback` 新增（Pydantic `CounterExampleCreate`，无 store 时 503，缺字段 422，pattern 去重继承自 `FeedbackStore.add`）；`architecture.md §8` 先行登记新端点（architecture-first）；`api.createFeedback` 新增；`ReviewQueue` 的 Mark wrong 不再静默 POST Review，而是弹 `MarkWrongModal` 收集 `correct_target` + 可选泛化 `pattern`，提交时并发 POST CounterExample + rejected Review；对齐 architecture.md §5 审阅交互（"标记错误时 → 可填写正确目标 → 触发反例生成"）+ §3 反馈机制 step 1；pytest 130/130 绿（新增 4 个 POST feedback 测试），`npm run build` 绿。
- 2026-05-13 · api+frontend: 反例 dedup 信号透出到审阅者——`FeedbackStore.add()` 签名从 `-> None` 改为 `-> bool`（True=新增 / False=merge 到已有 pattern）；`POST /api/v1/feedback` 响应新增 `deduplicated` + `total` 两个加法字段（非破坏性）；`CounterExampleCreateResult` TS 类型新增；`ReviewQueue` 提交反例后渲染顶部横幅——new=绿色"New counter-example pattern saved"、dedup=琥珀色"Merged into existing counter-example pattern"，均显示 pattern + 当前库大小；对齐 architecture.md §3 反馈机制 steps 3-5（相似 → 总结合并 / 不相似 → 新增），兑现北极星指标 #5（反例命中——是否都在 UI 上可见）；pytest 130/130 绿，`npm run build` 绿。
- 2026-05-13 · frontend: ReviewQueue 行级状态可见性 + 状态筛选——抽出 `<GapStatusChips>` 共享组件（行内 sm / 详情 md 两档），Type 列下方新增 status/retries/N 两个彩色 chip（status=unresolvable 红底 + ring、retries≥3 红、>0 琥珀色），顶部新增 `All (n) / Pending (n) / Unresolvable (n)` 三键状态筛选，`filteredGaps` 同时套用文本 + 状态 filter，missing status 默认为 "pending"；对齐 architecture.md §3 UnresolvedCall 生命周期（retry_count ≥ 3 → status="unresolvable"），兑现北极星指标 #1（单 GAP 审阅耗时——免点击）+ #5（state transparency——agent 放弃的 GAP 一眼可见）；pytest 130/130 绿，`npm run build` 绿。
- 2026-05-13 · api+frontend: Dashboard Unresolvable 背包可见性——`/api/v1/stats` 新增 `unresolved_by_status: {pending, unresolvable}` 分桶（遍历 `store._unresolved_calls.values()`，missing status 归入 "pending"），`Stats` TS 类型同步加可选字段；Dashboard 从 5 列扩到 6 列 StatCard，新增 `Unresolvable` 卡（`unresolvableGaps > 0` 时切 red-50 底 + red-700 字的 alert tone），`Unresolved GAPs` hint 从固定 "needs repair" 改为 `"{pending} pending · {unresolvable} unresolvable"` 动态分解；`StatCard` 扩展 `tone` prop；architecture.md §8 同步标注 stats 分桶字段；新增 2 个测试（空桶 + pending/unresolvable 混合）；对齐 architecture.md §3 GAP 生命周期 + §8 stats 契约，兑现北极星指标 #5（Agent 放弃的 GAP 在 Dashboard 顶部一眼可见）；pytest 131/131 绿，`npm run build` 绿。

---

## Key references

- Architecture：[`docs/architecture.md`](docs/architecture.md) — 13 章 + 56 条 ADR
- E2E Plan：[`docs/e2e-test-plan.md`](docs/e2e-test-plan.md) — 7 场景 + CastEngine 基线
- Gap analysis ADRs：[`docs/adr/0001-gap-analysis-corrections.md`](docs/adr/0001-gap-analysis-corrections.md) · [`0002-gap-analysis-round2.md`](docs/adr/0002-gap-analysis-round2.md) · [`0003-gap-analysis-round3.md`](docs/adr/0003-gap-analysis-round3.md)
- Agent skills docs：[`docs/agents/`](docs/agents/) — issue-tracker / triage-labels / domain
- 复用的上游仓：AI4CParser（解析）· codewiki_lite（source 点 REST）· codemap（Neo4j 存储层原型）
