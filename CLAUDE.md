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
- 2026-05-13 · api+frontend: Dashboard LLM-修复边背包可见性——`/api/v1/stats` 新增 `calls_by_resolved_by` 分桶（遍历 `store._calls_edges` 按 `e.props.resolved_by` 计数，涵盖 `symbol_table / signature / dataflow / context / llm` 五档），`Stats` TS 类型同步加可选字段；Dashboard 从 6 列扩到 7 列 StatCard，新增 `LLM Repaired` 卡（`llmCalls > 0` 切 amber-50 底 + amber-700 字的 warn tone，hint "needs review (resolved_by=llm)"），`Resolved Calls` hint 从 "{pct}% resolved" 升级为 "{pct}% resolved · {N} via llm"；`StatCard` `tone` 扩展 `warn` 档；architecture.md §8 stats 行标注新分桶；新增 2 个测试（空桶 + 四边混合 symbol_table/llm×2/signature）；对齐 architecture.md §4 CALLS 边属性 + §5 审阅对象（单条 CALLS 边，特别是 resolved_by='llm' 的）+ §8 stats 契约，兑现北极星指标 #2（调用链可信度可见性——llm 修复的边总量在 Dashboard 顶部一眼可见）；pytest 132/132 绿，`npm run build` 绿。
- 2026-05-13 · frontend: Dashboard→ReviewQueue 一键 drill-down——`StatCard` 扩展可选 `to` prop，带 `to` 时整个卡片成为 `<Link>`（hover 升阴影 + 边框转蓝、标题尾追加 `›` affordance）；`Unresolved GAPs` → `/review?status=pending`，`Unresolvable` → `/review?status=unresolvable`；`ReviewQueue` 引入 `useSearchParams`：mount 时读 `?status=` 作为 `statusFilter` 初值（仅白名单 `all | pending | unresolvable`）、用户切换筛选时反写 URL（`all` 删 key、其它值 `set`），再加一枚反向 effect 监听 URL 外部变化保持一致；架构先行登记到 `docs/architecture.md §5 跨页面 drill-down 契约`（StatCard `to` 可选 + URL 参数约定 + 宽松解析）；兑现北极星指标 #1（从"看到 backlog"到"聚焦审阅列表"的点击数 ≥2→1）+ #5（筛选状态可分享/可书签）；pytest 132/132 绿，`npm run build` 绿。
- 2026-05-13 · frontend: ReviewQueue→FeedbackLog 反例深链——"反例已保存" 横幅的 pattern span 升级为 `<Link to="/feedback?pattern=<encoded>">`（保留 font-mono + dotted underline，hover 转实线）；`FeedbackLog` 引入 `useSearchParams` 读 `?pattern=`，`useMemo` 解析匹配 index（命中首条同名 pattern），命中卡片加 `ring-2 ring-blue-400 border-blue-400` 高亮 + `scrollIntoView({behavior:'smooth', block:'center'})`；`cardRefs`（`Map<string, HTMLElement>`）负责按卡片 key 取 DOM；顶部新增 info 横幅——命中时蓝色"Highlighting pattern from ReviewQueue"，stale 链接（pattern 不在库）琥珀色"Pattern not found in current library"+ 原始 pattern；"Clear highlight" 按钮 `replace: true` 删 URL key；架构先行扩展 `architecture.md §5 跨页面 drill-down 契约` 增加 `/feedback?pattern=` 路由；兑现北极星指标 #5（反例命中—审阅者当场确认"下一轮注入 repair CLAUDE.md 的 pattern 就是这条"）+ 候选优化方向 #5（反例可视化）；pytest 132/132 绿，`npm run build` 绿。
- 2026-05-13 · api+frontend: 左侧导航 Feedback 活体计数 chip——`/api/v1/stats` 新增 `total_feedback` 字段（读 `app.state.feedback_store.list_all()`，store 未接时 graceful 归 0），`Stats` TS 类型加可选 `total_feedback?: number`；`App.tsx` 抽出 `NavItem` 类型（新增 `badgeKey?: 'total_feedback'` 可扩展字段），顶层 5s 轮询 `api.getStats()` 填充 `badges` 字典（failed poll 静默，不清零 — Dashboard 会报错），标注 `badgeKey` 的 NavLink 右侧追加 pill chip（0=gray-100/500、>0=amber-100/800、hover title 显示 "{N} counter example(s) in library"）；架构先行扩展 `architecture.md §8` 标注 `total_feedback` 字段用途；新增 2 个 api 测试（空 store=0 + 2 条反例=2）；对齐 architecture.md §3 反馈机制 + §8 stats 契约，兑现北极星指标 #5（反例库增长无需进 FeedbackLog 就能看到）+ 候选优化方向 #4（进度与可观测性—nav 级别的轻量活体信号）；pytest 133/133 绿，`npm run build` 绿。
- 2026-05-13 · frontend: 左侧导航 Review 活体 backlog chip——`App.tsx` 把 NavItem 契约从单用途 `badgeKey` 升级为纯函数 `deriveBadge(stats) => BadgeSpec | null`，每个 nav 项自己决定 count/tone/title；新增三档调色板 `TONE_CLASSES`（default=gray-100/500、warn=amber-100/800、alert=red-100/800）；Review chip：`unresolved_by_status.unresolvable > 0` → alert tone（title 报"agent gave up on N GAPs"）、只剩 pending → warn tone、全零 → default；bucket 缺失时用 `total_unresolved - unresolvable` 兜底保证旧后端也能显示；Feedback chip 迁到同契约保留原语义；顶层 state 从 `badges: Record<string, number>` 重构为 `stats: Stats | null` 让每个 deriveBadge 都能读任一字段；无需改后端或架构（仅 surface affordance）；对齐 architecture.md §3 UnresolvedCall 生命周期（pending / unresolvable 二态），兑现北极星指标 #1（drill-down 前先在任何页面都能看到 backlog）+ #5（状态透明度—unresolvable 在 nav 一眼红色）+ 候选优化方向 #4（nav 级别活体信号）；pytest 133/133 绿，`npm run build` 绿。
- 2026-05-13 · frontend: 左侧导航 Review chip 自动 drill-down——`BadgeSpec` 扩展可选 `to?: string` 字段，Review 的 `deriveBadge` 在 alert tone 返回 `to: '/review?status=unresolvable'`、warn tone 返回 `to: '/review?status=pending'`、default tone 不设 `to`；NavLink 渲染层 `const to = badge?.to ?? item.path`——默认 tone 仍指 `/review`，warn/alert 自动指向预筛选子视图；架构先行扩展 `docs/architecture.md §5 跨页面 drill-down 契约` 增加"左侧导航的活体 chip 在 alert/warn tone 下自动把 NavLink 指向对应的预筛选子视图"约定，复用已有的 `?status=<value>` query 约定，无需新增契约；让"看到红色 chip → 1 次点击 → 落到 agent 放弃的 GAP 列表"从任意页面都成立；兑现北极星指标 #1（审阅耗时—任一页面到 unresolvable 列表 ≥2→1 次点击）+ #5（状态透明度的可操作性）+ 候选优化方向 #4（进度与可观测性—nav 活体信号闭环到审阅列表）；pytest 133/133 绿，`npm run build` 绿。
- 2026-05-13 · frontend: ReviewQueue 空态区分真空 vs 过滤隐藏——原来一句 "No unresolved GAPs." 无法告诉审阅者图真的干净了还是筛选器藏住了。自 nav 自动 drill-down 落地后，从任意页面点到 `/review?status=unresolvable` 发现 0 条的概率上升（chip 轮询滞后、或点进来那一刻 backlog 刚被清掉）。新空态按三分支渲染：`gaps.length===0` → 绿色 "All GAPs resolved." + 提示到 Dashboard 触发修复；有 gaps 但 filteredGaps=0 → 中性灰面板列出被哪个 filter 隐藏了多少条（`status=` / `search=` / 两者并存 font-mono 标注）+ 居中 "Show all statuses" / "Clear search" 按钮分别调 `setStatusFilter('all')` / `setFilter('')`；纯 surface 优化不动 architecture.md §5 契约（drill-down 仍遵循 `?<filterName>=<value>` 宽松解析，只是优化落地页面的 empty affordance）；兑现北极星指标 #1（审阅者看到空列表无需猜原因）+ #5（状态透明度）+ 候选优化方向 #7（空状态友好化）；pytest 133/133 绿，`npm run build` 绿。
- 2026-05-13 · frontend: CallGraphView 图例变交互式 resolved_by 过滤器——原 Legend 仅是 `pointer-events-none` 静态图注。抽出共享 `LEGEND_ITEMS` 数组（color/dashed/marker 单一数据源，同时喂给 Legend DOM 渲染 + 可见性 effect 的 class 选择器），每行升级为 `role="switch"` + `aria-checked` 的 toggle 按钮：隐藏时 `opacity-40` + 文本加删除线，未隐藏时正常色；`hiddenKeys: Set<FilterKey>` state 由 `toggleKey` 翻转（`FilterKey` 覆盖 5 档 resolved_by + `unresolved` 合计 6 档），`counts` useMemo 在 `graph` 变化时按 bucket 计数并**永远显示**（含 llm 被隐藏时仍亮出"llm: 12"），右上角 "show all" 按钮在任一 bucket 隐藏时出现；新增 useEffect `[hiddenKeys, elements]` 在 cytoscape 实例上按 class 选择器切 `display: 'none'|'element'`（`edge.resolved.<key>` + `node.unresolved, edge.unresolved`），O(bucket) 而非重建元素集；纯 surface 优化不动 architecture.md §2 `resolved_by` 枚举契约（"新增需同步 schema + 前端过滤器"——此改动等于把前端过滤器兑现出来）；兑现北极星指标 #2（调用链可信度可见性——isolate llm-repaired 从"肉眼扫 ★"降到"一键关闭其它 5 档"）+ 候选优化方向 #2（按 resolved_by 视觉语言）；pytest 133/133 绿，`npm run build` 绿。
- 2026-05-13 · frontend: Dashboard 非 backlog StatCard 也承载 drill-down——`Source Points` → `/sources`、`Files` → `/functions`、`Functions` → `/functions`，复用已有的 `StatCard.to` prop（Link 包裹 + 标题尾 `›` affordance + hover 转蓝边框 + focus ring），无需改 StatCard 本体；架构先行扩展 `docs/architecture.md §5 跨页面 drill-down 契约`——新增 bullet 明确"非 backlog 类 StatCard 也承载导航意图"+ "目标子视图无需预筛选时 `to` 直接指向列表根路径"+ "`to` 约定是轻量 affordance（右侧 `›` 暗示可点），不强制所有 StatCard 都带"，legitimize 把 drill-down 从仅 backlog（Unresolved / Unresolvable → /review）扩展到浏览类（Source/Function browsers），同一 affordance 语言覆盖全卡；兑现北极星指标 #1（从 Dashboard 1 次点击落到对应浏览视图，减少审阅者"看到数字 → 找到 nav → 切页"的路径）+ 候选优化方向 #4（进度与可观测性—Dashboard 作为全站 hub）；pytest 133/133 绿，`npm run build` 绿。
- 2026-05-13 · frontend: ReviewQueue caller → CallGraphView 深链——`ReviewQueue` Caller 单元格从纯文本升级为 `<Link to="/graph?function=<encoded caller_id>">`（font-mono + dotted underline，hover 转实线，`onClick` stopPropagation 避免触发行选中闪背景色）；`CallGraphView` 已经消费 `?function=` 并调 `api.getCallChain(id, depth)` 高亮 root，消费侧零改动；架构先行扩展 `docs/architecture.md §5 跨页面 drill-down 契约`——新增 bullet 明确 ReviewQueue caller cell deep link 约定，复用已有 `?function=` 约定不新增 surface；把 "审阅这条 GAP → 看它在调用链里是哪条边" 从"复制 id → 切页 → 粘贴 URL"压成一次点击，兑现北极星指标 #1（GAP 审阅耗时—调用链上下文 ≥多步 → 1 次点击）+ #2（调用链可信度可见性—审阅时 llm 修复的 edge 在图里是哪条，CallGraphView 图例已经按 resolved_by 上色 + llm 虚线 + ★）+ 候选优化方向 #1（审阅面板信息密度—相邻调用链一键跳转）；pytest 133/133 绿，`npm run build` 绿。
- 2026-05-13 · frontend: FunctionBrowser 每行函数挂 GAP count chip → `/review?caller=<id>`——新增 `GapChip` 子组件（0=不渲染、1-2=amber、3+=red），`useEffect` 同时并发 `api.getFunctions` + `api.listUnresolved(500)` 后 `Map<caller_id, number>` 聚合；函数表格新增 `GAPs` 列，左侧文件树每个文件同步显示 `fileGapSums` 小 pill（同样 0 隐藏、1-2 amber、3+ red）让审阅者一眼看到哪个文件背包最重；`ReviewQueue` 增加 `callerFilter` state + `?caller=` bidirectional URL sync effect（镜像 `?status=` 的现有模式），`filteredGaps` 扩展 `byCaller` 精确匹配，顶部新增"Filtering by caller: <name> × Clear"蓝色 chip 及 filter-hidden 空态扩展（三档 status/caller/search 组合拼接 + 对应一键清除按钮）；architecture.md §5 先行登记 FunctionBrowser GAP chip → `/review?caller=<id>` 契约条目；复用已有 `?<filterName>=<value>` 约定，不新增 surface；兑现北极星指标 #1（从"看函数浏览器发现某个函数 backlog 多 → 跳预筛选 GAP 列表"从"切页 → 手敲 caller_id"压到一次点击）+ #2（哪个函数 backlog 最重一眼可见）+ #5（backlog 在函数维度的分布可见）+ 候选优化方向 #4（进度与可观测性—函数维度的活体信号）；pytest 133/133 绿，`npm run build` 绿。
- 2026-05-13 · frontend: Dashboard 新增 "Top backlog functions" widget——`refresh` 回调扩展并发拉 `api.getFunctions` + `api.listUnresolved(500, 0)`（两者 `.catch` 归零保证 StatCards 不受影响），客户端 `Map<caller_id, number>` 聚合后 `useMemo` 降序取前 5，每行渲染为 `<Link to="/review?caller=<encoded caller_id>">`（等号 + 函数名 + GAP count pill tri-tone amber≥1/red≥3 + `›` affordance，hover 浅灰底）；顶部 subtitle 显示"top N of M"（M=带 GAP 的函数总数）；空态友好提示"Run the repair agent to surface per-function backlog"；架构先行扩展 `docs/architecture.md §5 跨页面 drill-down 契约` 增加 Dashboard top-backlog widget → `/review?caller=<id>` 契约条目，复用已有 `?caller=` 约定，不新增 surface；兑现北极星指标 #1（"打开 Dashboard → 发现热点函数 → 跳预筛选 GAP 列表"从"切去 FunctionBrowser → 找到函数 → 点 chip"压到一次点击）+ #5（状态透明度—Dashboard 作为全站 hub 直接暴露热点函数，无需绕路）+ 候选优化方向 #4（进度与可观测性—Dashboard 级函数维度 backlog 排行）；pytest 133/133 绿，`npm run build` 绿。
- 2026-05-13 · frontend: CallGraphView NodeInspector → ReviewQueue 深链——补齐前端 drill-down 网格里最后没参与的一页（原 inspector 只在 function 分支有 "Center here"，unresolved 分支连一个出口都没有，审阅者从图里看到可疑节点/GAP 后只能手抄 caller_id 切页粘贴）。function-node 分支：父级 `useMemo` 从 `graph.unresolved` 按 `caller_id` 客户端聚合得 `gapCountByCaller: Map<string, number>`（不发额外请求，与当前子图视图一致），传入 inspector 后在 "Center here" 下方条件渲染 `<Link to="/review?caller=<encoded fn.id>">Review GAPs ({count})</Link>`（tri-tone amber≥1/red≥3 与 FunctionBrowser/Dashboard chip 共享视觉语言，0 时改渲染灰色 "No unresolved GAPs in this subgraph" 避免空跳转）；unresolved-node 分支：candidates 列表下方挂 `<Link to="/review?caller=<encoded gap.caller_id>">Review this caller</Link>`（amber pill + `›` affordance）——从图里看到可疑 GAP 时一键回到预筛选 GAP 列表；架构先行扩展 `docs/architecture.md §5 跨页面 drill-down 契约` 增加 CallGraphView inspector → `/review?caller=<id>` 契约条目（function 分支 count 约定 + unresolved 分支 caller_id 约定），复用已有 `?caller=` 约定，不新增 surface；import 从单 `useSearchParams` 扩到 `Link, useSearchParams`；兑现北极星指标 #1（调用链视图 → GAP 审阅从"手抄 id + 切页"压成一次点击）+ #2（调用链可信度可见性——图视图与审阅队列的审阅上下文互通）+ #5（状态透明度——调用链里每个节点都能看到自身 backlog）+ 候选优化方向 #1（审阅面板信息密度—相邻调用链跳转）；pytest 133/133 绿，`npm run build` 绿。
- 2026-05-13 · frontend: SourcePointList 每行挂 repair 进度列——5s 轮询 `api.getAnalyzeStatus()` 把 `sources: SourceProgress[]` 聚合成 `Map<source_id, SourceProgress>`（failed poll 静默保留上次快照，与 App.tsx nav-chip 同节奏、比 Dashboard 2s 稍慢——browsing 页面而非 operations 页面），新增 `Progress` 列渲染 `<SourceProgressCell>`：`gaps_total===0` 或缺进度文件 → 灰色 `—` 避免"0/0 看起来像 done" 歧义；有进度 → `gaps_fixed/gaps_total` + pct + 1.5rem mini bar（done 绿 / 进行中蓝）+ 状态行（`done` 绿 / `current: <gap>` 带 font-mono / `idle` 灰），与 Dashboard `SourceProgressCard` 共用视觉语言；colSpan 从 6 升到 7，纯 surface 优化不改后端（`AnalyzeStatus.sources[]` 已经在 architecture.md §3 Repair Agent 进度文件契约 + §8 analyze/status schema 登记，此 tick 只是前端消费）；让审阅者在 SourcePointList 就能 triage "哪些 source 点已修完、哪些还在跑、哪些未被访问" 不必切 Dashboard 找对应卡片；兑现北极星指标 #5（状态透明度——每个 source 点的进度在列表里直接可见）+ #1（审阅耗时——triage source 点进度从"切到 Dashboard 找对应卡"压到一眼看列）+ 候选优化方向 #4（进度与可观测性——source 维度的活体信号）；pytest 133/133 绿，`npm run build` 绿（4.77s）。
- 2026-05-13 · repair+docs: progress.json 字段对齐（ADR 0004）——修复 Tick #24 落地后发现的**生产破坏性 bug**：`log_notification.py` hook 输出为 `{fixed_gaps, total_gaps, current_gap_id}`（ADR 0001 H5 / implementation-plan §2.4 的旧拼写），而所有消费方（`api/routes/analyze.py`、`api/client.ts SourceProgress`、`Dashboard`、`SourcePointList`、`cli.py status`、`test_api.py` fixtures）按 `{gaps_fixed, gaps_total, current_gap}` 读取——导致生产环境下 SourcePointList Progress 列与 Dashboard Repair Progress 卡永远渲染 `—` / 零态。修正方向选择"hook 向 REST 契约看齐"（外契约更贵）：hook 输出改 canonical schema，同时 `event.get("gaps_fixed", event.get("fixed_gaps", 0))` 双读旧事件键保兼容；`test_hooks.py` 主测试改 canonical schema + 新增 `test_log_notification_accepts_legacy_event_keys`；`architecture.md §3` 把先前只有自然语言描述的进度文件从散文升级为显式 JSON schema（锁住字段名）；新增 `docs/adr/0004-progress-json-schema-correction.md` 记录分叉原因、为何修 hook 而不是修 5 处消费方、为何保留双读；`docs/adr/0001-gap-analysis-corrections.md §H5` 与 `docs/implementation-plan.md §2.4` 加 ADR 0004 指针。对齐 architecture.md §3 Repair Agent 进度文件契约；pytest 134/134 绿（133 + 1 新增 legacy-compat 测试），`npm run build` 绿。
- 2026-05-13 · repair+frontend: ReviewQueue 暴露 retry 审计字段——Tick #25 让审阅者在 GapDetail 看到"最后一次修复失败的时间 + 原因"（之前只有 `retry_count/3` chip，想知道为什么失败得手翻 `logs/repair/<source_id>/*.jsonl`）。`architecture.md §3` 先行扩展——retry 流程图加入 `last_attempt_timestamp = ISO-8601 now` + `last_attempt_reason = <category>: <summary>` 两行，新增 "Retry 审计字段" 段落锁死契约（`<category> ∈ {gate_failed, agent_error, subprocess_timeout, subprocess_crash}`，reason ≤200 字）；§4 `UnresolvedCall` schema 表列末尾补 `last_attempt_timestamp, last_attempt_reason`。实现：`UnresolvedCallNode` 加两个 `str \| None` 可选字段（默认 None，frozen=True 不破坏哈希）；`GraphStore` protocol 新增 `update_unresolved_call_retry_state(call_id, timestamp, reason)`，`InMemoryGraphStore` 实现为 frozen 副本替换、`Neo4jGraphStore` 补 stub；`RepairConfig` 加 `graph_store: GraphStore \| None = None`，`_run_single_repair` 每次 `_check_gate` 返回 False 后调 `_record_retry_attempt`（新 helper，用 `datetime.now(timezone.utc).isoformat()` 打 ISO-8601 UTC，并通过每次 repair 运行级 `_reachable_cache: dict[str, set[str]]` 把 stamp 限制在 source_id 可达子图里的 GAP，避免跨 source 错伤）。前端：`UnresolvedCall` TS 接口补两个可选字段；`GapDetail` 加 last-attempt 面板——按 `<category>:` 前缀切 tone（gate_failed=amber、agent_error/subprocess_*=red、其它=gray），ISO-8601 转本地时间显示（fallback 原样），hover title 仍显示原串。测试：`test_repair_orchestrator.py` 加 `test_orchestrator_stamps_retry_audit_on_gate_failure`（gate 永远失败 → 3 次 attempts → 字段被 stamp，reason 以 `gate_failed:` 起）+ `test_orchestrator_noop_retry_stamp_when_graph_store_missing`（无 graph_store 时 silent noop）；`test_graph_store.py` 加 `TestUpdateUnresolvedCallRetryState`（更新+missing id noop）。对齐 architecture.md §3 Retry 审计字段 + §4 UnresolvedCall schema，兑现北极星指标 #1（审阅耗时——看失败原因从"翻 JSONL"压到一眼）+ #5（状态透明度——agent 每次放弃的原因都在 UI 上）+ 候选优化方向 #1（审阅面板信息密度—last-attempt 面板）；pytest 138/138 绿（134 + 4 新增），`npm run build` 绿。
- 2026-05-13 · repair: 非门禁 subprocess 失败不再杀 retry loop——Tick #25 落地时只处理了 `gate_failed`，但 architecture.md §3 Retry 审计字段明明列了四档 category（`gate_failed / agent_error / subprocess_timeout / subprocess_crash`）。实操中有个默默咬人的边界：如果 `RepairConfig.command` 指向不存在的二进制（ops 配置错了 / 路径漂移 / dev 没装 `claudecode`），`asyncio.create_subprocess_exec` 第一次就抛 `FileNotFoundError`，异常冒出 `_run_single_repair`——**没 stamp、没 retry、没 SourceRepairResult**，相当于整条 source 静默消失；刚上线的 GapDetail 审计面板永远看不到"为什么跑不起来"，直接架空 Tick #25 兑现的北极星指标 #5。先行扩展 `architecture.md §3 Retry 审计字段` 加一句"非门禁失败同样记账"——捕获后按同契约 stamp 对应 category，**继续走 retry 循环直到用完预算，不允许让异常冒出 `_run_single_repair` 杀掉该 source 的重试**。实现：`_run_single_repair` 把 `asyncio.create_subprocess_exec` + `proc.communicate()` 包一层 `try/except (OSError, FileNotFoundError)`（`FileNotFoundError` 是 `OSError` 子类，双挂纯为意图显性），catch 分支调 `_record_retry_attempt(reason=f"subprocess_crash: {type(exc).__name__}: {exc}")` 后 `continue` 回 while 循环，外层 `try/finally` 的 `_cleanup_injection` 照常执行；新增 `_truncate_reason()` 模块级 helper 兜住 architecture 锁的 200 字上限（超长时末尾换 `…`）。测试：`test_orchestrator_handles_subprocess_spawn_failure` 把 `command` 指向 `/nonexistent-binary-codemap-test-xyz`，wire 一个 `InMemoryGraphStore` + 一个 GAP，断言 success=False、attempts=3、`last_attempt_reason.startswith("subprocess_crash:")`、长度 ≤200、`_check_gate` 从未被 call（把"spawn 失败后不该走到 gate"做成 regression guard）；AsyncMock 换成 module-level 实例变量以便 `assert_not_called()`。对齐 architecture.md §3 Retry 审计字段（非门禁失败分支 + 4 档 category），兑现北极星指标 #5（状态透明度——CLI 二进制缺失这类 ops 故障能落到 GapDetail 红色 subprocess_crash 面板而不是静默消失）+ 候选优化方向 #4（进度与可观测性—orchestrator 一次性失败也能出现在 UI）；pytest 139/139 绿（138 + 1 新增），`npm run build` 绿。
- 2026-05-13 · repair: Agent 非零退出不再被误贴 gate_failed——Tick #26 把 spawn-time 异常分到 `subprocess_crash`，但四档 category 里 `agent_error` 还没人认领；实操里最常见的场景正是 **agent 成功起来了，但任务跑挂** —— `claudecode` 配额用完退 1、`opencode` LLM 超时退 124、目标仓库里 hook 脚本 `SyntaxError` 退 2、各类 "agent 起来了但没写边" 的失败统统以非零 returncode 落地。之前没检查 `proc.returncode`，非零退出直接 fall-through 到 `_check_gate`——门禁看到还有 pending GAP 就 stamp `gate_failed: remaining pending GAPs`，审阅者在 GapDetail 看到琥珀色 gate_failed 面板，误以为"agent 干过活但门禁把关住了"，实际 agent 压根没跑完——**把 ops 故障伪装成业务信号**，和 Tick #26 防的 "silent kill" 是一个硬币的两面。修正：`_run_single_repair` 在 `finally` 关闭 log_fh 之后、`_check_gate` 之前插一个 returncode 门，`proc.returncode is not None and != 0` 时调 `_record_retry_attempt(reason=_truncate_reason(f"agent_error: exit {rc}"))` + `continue`，短路掉门禁检查；`exit N` 格式让 reviewers 在 GapDetail 一眼读出 signal（`exit 124` → timeout、`exit 1/2` → 通用错、`exit 127` → command not found fallback），不需要翻 `logs/repair/<sid>/<sid>.attempt{N}.log`。测试：`test_orchestrator_stamps_agent_error_on_nonzero_exit` 用 `sh -c 'exit 7'` 触发非零退出（比 `false` 更显性、比 missing binary 更精准——那条走的是 subprocess_crash），断言 success=False、attempts=3、`reason.startswith("agent_error:")`、包含 `"exit 7"`、**不以 gate_failed 起**（反向 regression guard，锁住"不能再误贴 gate_failed"的修正意图）、长度 ≤200、`_check_gate` 从未被 call。对齐 architecture.md §3 Retry 审计字段（把 4 档 category 中 agent_error 这档兑现到位），兑现北极星指标 #5（状态透明度——agent 起来了但失败的具体退出码直接在 UI 上，ops 信号不再被门禁失败覆盖）+ 候选优化方向 #4（进度与可观测性—agent 运行时失败与门禁失败在 UI 上彻底分流）；pytest 140/140 绿（139 + 1 新增），`npm run build` 绿。
- 2026-05-13 · repair: 挂死 agent 不再静默耗尽 source retry 预算——补齐 4 档 Retry 审计字段 category 里的最后一档 `subprocess_timeout`（前三档 `gate_failed`/`subprocess_crash`/`agent_error` 分别由 Tick #25/#26/#27 兑现）。架构原先开篇就写"超时：不限时，Agent 自然完成"，但现实里 LLM 后端 stall、网络挂死、agent 进入死循环时，`proc.communicate()` 会无限等——**3 次 retry 预算全被同一个挂死进程吃掉，GapDetail 面板空空如也，Dashboard 的 SourceProgressCard 永远停在同一个 current_gap**，和 Tick #26/#27 防的"silent kill / ops 伪装"是同一类隐患：signal 丢失。修正走 opt-in 不破坏原契约：`architecture.md §3` 先行新增"超时护栏（subprocess_timeout）"段落——`RepairConfig.subprocess_timeout_seconds: float \| None = None`（默认 `None` = 不限时，与原契约兼容），显式配置为正数时 Orchestrator 用 `asyncio.wait_for` 包 `proc.communicate()`、到点抛 `asyncio.TimeoutError` → `proc.kill()` + `await proc.wait()` 回收 → stamp `last_attempt_reason = "subprocess_timeout: <N>s"` → `continue` 进下一次 retry；明确把三类失败在 UI 上分流（超时=ops 信号 / agent_error=跑挂 / subprocess_crash=spawn 失败）。实现：`RepairConfig` 加可选字段；`_run_single_repair` 在 `create_subprocess_exec` 之后按 `timeout is not None` 分两支——有 timeout 时 `await asyncio.wait_for(proc.communicate(), timeout=timeout)`，catch `asyncio.TimeoutError` 后 `proc.kill()` + `await proc.wait()`（wait 的 secondary exception 用 `try/except Exception: pass` 吞住，避免 cleanup 本身冒出异常杀死 retry loop），然后 stamp + `continue`；无 timeout 走原逻辑 `await proc.communicate()`。测试：`test_orchestrator_stamps_subprocess_timeout_on_hung_agent` 用 `command="sh"` + `args=["-c", "sleep 5; :"]` + `subprocess_timeout_seconds=0.2` 触发——**注意不能用裸 `sleep 5`**：`_build_command` 会把 prompt 作为最后一个 arg 追上去，`sleep 5 <prompt>` 会被 sleep 以"invalid time interval"退 1、被贴成 `agent_error: exit 1`；改成 `sh -c 'sleep 5; :'` 让 prompt 落到 `$0` 被忽略才精准触发 timeout 分支（doc-comment 留注释避免后人踩坑）；断言 success=False、attempts=3、`reason.startswith("subprocess_timeout:")`、包含 `"0.2s"`、**不以 gate_failed/agent_error/subprocess_crash 起**（三条反向 regression guard 锁住 4 档 category 彼此不混淆）、长度 ≤200、`_check_gate` 从未被 call。加 `test_orchestrator_no_timeout_when_not_configured` 保 backwards-compat——默认 `None` 时用 `echo done` 跑到底，attempts=1、success=True。对齐 architecture.md §3 Retry 审计字段 + 超时护栏（4 档 category 全部兑现到位），兑现北极星指标 #5（状态透明度——挂死 agent 的 ops 信号能在 GapDetail 红色 subprocess_timeout 面板上直接看到，不再被"current_gap 卡住"伪装成正常运行）+ 候选优化方向 #4（进度与可观测性—wedged 进程再也不能烧掉整个 source 预算且无可见信号）；pytest 142/142 绿（140 + 2 新增），`npm run build` 绿（4.75s）。
- 2026-05-13 · frontend: GapDetail last-attempt 面板 4 档分色——Tick #25 落地时把 4 档 `<category>` 简单二分（gate_failed=amber / 其它 3 档=red），但 architecture.md §3 超时护栏明文写"超时=ops 信号 / agent_error=agent 跑挂 / subprocess_crash=spawn 失败，**三档在 UI 上彻底分流**"，3 档共用同一 red tone 直接违反架构契约——审阅者看到红色面板只知"失败了"，区分不出该找 ops（查 CLI 路径 / 查 LLM 后端是否挂了）还是找 agent 侧业务 bug（查 hook 脚本 / 查配额）。先行扩展 `architecture.md §5` 新增"GapDetail last-attempt 分色"段落，把 4 档 tone 锁死：`gate_failed`=amber（软失败，下一轮可能推进）、`agent_error`=red（agent 逻辑失败）、`subprocess_crash`=**fuchsia**（spawn 失败，ops 配置问题）、`subprocess_timeout`=**orange**（ops stall，LLM/网络/死循环），unknown/legacy=gray fallback；fuchsia 与 orange 在 Tailwind 默认调色板里与 red/amber 夹角够大，色盲友好度也过关。实现：`ReviewQueue.tsx` GapDetail 的 `categoryTone` 从 3 支（gate_failed/3 档合并/fallback）展开成 5 支（4 档独立 + fallback），注释同步 architecture.md §5 的锚点；纯 surface 优化不动后端、不动 schema；对齐 architecture.md §3 Retry 审计字段 + 超时护栏 + §5 GapDetail last-attempt 分色；兑现北极星指标 #1（审阅耗时——读 category 从"看红色后还要读 reason 文字判断档位"压到"看颜色直接知道去找谁"）+ #5（状态透明度——4 档 ops/agent 信号彻底分流在 UI 上）+ 候选优化方向 #1（审阅面板信息密度—视觉语言细化）；pytest 142/142 绿，`npm run build` 绿（4.84s）。

- 2026-05-13 · frontend: ReviewQueue 4 档 category 筛选器——Tick #29 把 4 档 retry category 的 GapDetail 面板上色分流，但审阅者想"isolate 所有 subprocess_timeout 的 GAP 看是不是 LLM 后端集体挂了"还是只能逐行滚、靠眼扫 4 色面板判断；与 `?status=` / `?caller=` 的 drill-down 语义不一致（后者一键把 backlog 切到单一 bucket）。先行扩展 `architecture.md §5 跨页面 drill-down 契约` 新增 `?category=<cat>` 条款：值 ∈ `{all, gate_failed, agent_error, subprocess_crash, subprocess_timeout}`，匹配 `last_attempt_reason` 的 `<category>:` 前缀；chip tone 必须**与 GapDetail last-attempt 分色同色系**（amber/red/fuchsia/orange），让 chip 列与详情面板共用一套视觉语言。实现：`ReviewQueue.tsx` 新增 `CategoryFilter` 类型 + `CATEGORY_FILTERS` + `isCategoryFilter` 类型守卫 + `extractCategory(reason)` 提取器（拆 `<cat>:` 前缀，reason 为空/无冒号时返回 null 避免把"还没 stamp 过"的 GAP 误归任何 bucket）；`categoryFilter` state 镜像 `statusFilter` 的初值读 `?category=` + URL 双向同步 effect + 外部 URL 变化监听 effect；`filteredGaps` useMemo 新增 `byCategory` 谓词（non-all 时 `extractCategory(g.last_attempt_reason) === categoryFilter`，null category 在 non-all bucket 一律不命中——reviewer 在问"agent_error 这批还剩多少没修"时，把"还没试过"的 GAP 算进去只会污染信号）；新增 `categoryCounts` useMemo 做 5 档总数；chip 行放在 Status chip 下面，`gate_failed` 按 `bg-amber-50/text-amber-800/border-amber-300`、`agent_error` red、`subprocess_crash` fuchsia、`subprocess_timeout` orange，active 态统一覆盖 `bg-blue-600 text-white border-blue-600` 与其它 chip 一致；filter-hidden 空态扩展第 3 档 `category=<cat>` 描述 + "Show all categories" 一键清除按钮。纯 surface 优化不动后端、不动 schema；复用 `?<filterName>=<value>` 既有约定不新增 surface；对齐 architecture.md §3 Retry 审计字段 4 档枚举 + §5 drill-down 契约 + §5 GapDetail last-attempt 分色（chip 与面板共色系）；兑现北极星指标 #1（审阅耗时——按 category 聚焦从"滚表眼扫"压到一次点击）+ #5（状态透明度——backlog 在 category 维度的分布可见）+ 候选优化方向 #1（审阅面板信息密度—筛选器细化）；pytest 142/142 绿，`npm run build` 绿（4.81s）。
- 2026-05-13 · api+frontend: Dashboard "Retry reasons" category 分布 chip 行——Tick #29（GapDetail 4 档分色）+ Tick #30（ReviewQueue `?category=` 筛选器）把 per-GAP 和 per-list 的 category 信号都打通了，但 Dashboard 作为全站 hub 只告诉审阅者"有 30 个 unresolvable"、**不告诉是因为 LLM 后端集体挂了（subprocess_timeout）还是 hook 脚本坏了（agent_error）还是配置路径漂了（subprocess_crash）**——相同数字、天差地别的 triage 方向。后端：`/api/v1/stats` 新增 `unresolved_by_category` 分桶，遍历 `store._unresolved_calls.values()` 按 `last_attempt_reason` 的 `<category>:` 前缀聚合；无 stamp / 无冒号 / 空前缀统一归 `"none"`，避免把未试过的 GAP 误混进任何 ops 桶。前端：`Stats` TS 接口新增 `unresolved_by_category?: Record<string, number>`；Dashboard 在 7-card StatCard grid 下方插入 "Retry reasons" 单行 chip，5 档按 §5 "GapDetail last-attempt 分色" 严格同色系（gate_failed=amber-100/800、agent_error=red-100/800、subprocess_crash=fuchsia-100/800、subprocess_timeout=orange-100/800、none=gray-100/700）+ hover 深一档，count=0 的 chip 不渲染避免视觉噪音；每个 chip 是 `<Link to="/review?category=<key>">`，与 Tick #30 已落地的 `?category=` 约定对接，让"打开 Dashboard → 一眼看到 30 个 unresolvable 里 25 个是 subprocess_timeout → 1 次点击落到预筛选列表"从"进 ReviewQueue 再逐行眼扫 4 色"压成一次点击；`categoryRowTotal===0` 时整行不渲染保证空态不添加空横条。架构先行：`architecture.md §8` stats 契约行扩展登记 `unresolved_by_category` 分桶（键枚举 + `none` 桶语义）；§5 drill-down 契约新增一条 bullet 明确 Dashboard Retry reasons chip 行的色系 + `?category=` 约定 + 复用性。测试：新增 `test_get_stats_unresolved_by_category`（4 档各 1 + subprocess_timeout×2 + 无 stamp×1 → 5 桶准确计数）+ `test_get_stats_unresolved_by_category_empty`（空 store 返回空 dict，前端无需 undefined 守卫）。对齐 architecture.md §3 Retry 审计字段 4 档 + §5 drill-down 契约 + §8 stats 契约；兑现北极星指标 #1（审阅耗时——从 Dashboard 到 ops triage 从"猜 + 切页逐行眼扫"压到一次点击）+ #5（状态透明度——agent 放弃原因的 5 档分布在 Dashboard hub 一眼可见）+ 候选优化方向 #4（进度与可观测性—Dashboard 作为全站 hub 暴露 category 维度的活体信号）；pytest 144/144 绿（142 + 2 新增），`npm run build` 绿（4.81s）。
- 2026-05-13 · repair+api+frontend: RepairLog 端到端落地（架构最大 gap 之一）——架构 §3 修复成功时承诺"创建 CALLS 边 + RepairLog 节点 + 删除 UnresolvedCall"、§4 给出 `RepairLogNode` schema、ADR #51 锁死"caller_id + callee_id + call_location 三元组定位该边的修复过程"，但实际链路完全断的：`icsl_tools.write_edge` 把 dict 直接塞 `store.create_repair_log()`，`InMemoryGraphStore` / `Neo4jGraphStore` 都没实现 `create_repair_log` / `get_repair_logs`，更没有 REST 端点把审计记录暴露给前端 CallGraphView——**审阅者点选 ★ 虚线 llm 边时根本拿不到"为什么 LLM 挑了这个 callee"的依据**，只能去翻 `logs/repair/<source_id>/*.jsonl`，违反 §5 审阅对象契约。本 tick 闭合后端 + API + 前端 client 三段，CallGraphView inspector 面板留给 Tick #33。修正：(1) `icsl_tools.write_edge` 把 dict-based 构造改为 typed `RepairLogNode`，timestamp 用 `datetime.now(timezone.utc).isoformat()`（schema 声明为 `str` 不再是 epoch float）+ 新签名加 `llm_response="" / reasoning_summary=""` kwarg 让静态分析路径继续 work；`RepairLogNode` lazy-import 自 `codemap_lite.graph.schema` 保证 subprocess CLI `--help` 不需要 codemap_lite 在 sys.path（agent sandbox 实际场景）。(2) `GraphStore` Protocol 加 `create_repair_log(node) -> str` + `get_repair_logs(caller_id, callee_id, call_location) -> list[RepairLogNode]` 两个方法；`InMemoryGraphStore` 加 `_repair_logs: dict[str, RepairLogNode]` + 三元组宽松过滤（None=不筛）；`Neo4jGraphStore` 加 NotImplementedError stubs 保 typing。(3) 新增 `codemap_lite/api/routes/repair_logs.py`：`GET /api/v1/repair-logs?caller=&callee=&location=` 走 `dataclasses.asdict()` 序列化（与 §4 schema 字段名 1:1）；`app.py` 注册 router + stats 增 `total_repair_logs` 字段读 `len(getattr(s, "_repair_logs", {}))`。(4) 前端 `client.ts` 加 `RepairLog` 接口（8 字段镜像 RepairLogNode）+ `Stats.total_repair_logs?: number` + `api.getRepairLogs({caller, callee, location})` 走 URLSearchParams 模式与 `getSourcePoints` 一致。测试：`test_graph_store.py::TestRepairLogPersistence` 4 项（create+retrieve / 三元组定位 / 单字段过滤 / no-match 空列表）；`test_api.py::TestRepairLogsEndpoint` 5 项（empty list / persisted / 三元组定位 / caller-only / `total_repair_logs` 在 stats 0→2）；`test_get_stats` 加 `total_repair_logs` in-keys 断言；`test_icsl_tools.py::test_write_edge_creates_calls_and_repair_log` 从 dict 下标改为属性访问 `.caller_id` / `.callee_id` / `.call_location` / `.repair_method`。架构契约对齐：§4 RepairLog schema + §5 CallGraphView RepairLog inspector 契约（前端 inspector 留 Tick #33）+ §8 `GET /api/v1/repair-logs` + `total_repair_logs` 字段 + ADR #51 属性引用契约；兑现北极星指标 #2（调用链可信度可见性——llm 修复的边从"可辨认（视觉语言）"升级为"可解释（审计链路存在 + 可查询）"）+ #5（状态透明度——cumulative repair 量在 Dashboard 可见，每条 llm 边的修复过程可通过三元组 API 拉到）；pytest 153/153 绿（144 + 4 RepairLog graph store + 5 RepairLog endpoint），`npm run build` 绿（4.86s）。
- 2026-05-13 · frontend: CallGraphView LLM 边 RepairLog inspector 面板（闭合 Tick #32 留下的最后一段）——Tick #32 把 RepairLog 端到端打通到 `api.getRepairLogs()`，但前端只在 Dashboard 露了个 `total_repair_logs` 总数；审阅者点 ★ 虚线 llm 边时 inspector 面板是空的，"为什么 LLM 挑了这个 callee" 仍然要去翻 `logs/repair/<source_id>/*.jsonl`，违反 architecture.md §5 line 373 RepairLog inspector 契约。本 tick 落地：(1) `buildElements()` 给 resolved-edge cytoscape data 多挂一份 `data.edge: CallEdge` 让 tap handler 拿到完整 (caller_id, callee_id, call_file, call_line) 三元组，避免再去 `graph.edges` 二次查找。(2) `cy.on('tap', 'edge.resolved.llm', ...)`：只对 llm 档边挂 inspector handler——其它 4 档 resolved_by 是确定性解析，没有审阅价值的 LLM 推理痕迹；非 llm 边点击不改变 `selected`。(3) 新增 `EdgeLlmInspector` 子组件——挂载时按三元组调 `api.getRepairLogs({caller, callee, location: \`${call_file}:${call_line}\`})`、`useEffect` 用 `cancelled` flag 防 race（依赖三元组任一变化即重拉）；渲染按 §5 契约：`formatTimestamp()` 用 `Number.isFinite(...) && /^\d+(\.\d+)?$/.test(ts)` 把 epoch-seconds 字符串转 `new Date(num*1000)`、ISO-8601 走 `new Date(ts)`，`Number.isNaN(d.getTime())` fallback 显示原串保鲁棒（架构明文"ISO-8601 或 epoch-seconds 都接受"）；reasoning_summary 缺失显示 "No reasoning summary recorded"；`llm_response` >320 字按 `LLM_RESPONSE_PREVIEW_CHARS` 截断 + "Show full response" 切换按钮（每条 log 独立 expanded state by `log.id`）+ `<pre>` 配 `max-h-72 overflow-auto` 不淹没面板；空数组分支显示"No repair log entries for this edge — the LLM resolution predates the RepairLog persistence rollout"，让审阅者一眼分辨"是 audit 没写"还是"加载失败"。(4) `NodeInspector` 加 `edge_llm` 分支直接代理 `EdgeLlmInspector edge={selected.data as CallEdge}`，与 4 档其它分支并列；选中状态清除走原 `cy.on('tap', evt => evt.target === cy && setSelected(null))`，无需新增 surface。纯 surface 优化不动后端、不动 schema、不新增契约——架构 §5 line 373 早在 Tick #32 就先行登记好了 inspector 渲染契约（"timestamp + reasoning_summary + 截断后的 llm_response + 展开折叠按钮"），本 tick 是 line 373 的实现兑现。对齐 architecture.md §5 RepairLog inspector 契约（line 373）+ §4 RepairLogNode schema + §8 `GET /api/v1/repair-logs` + ADR #51 三元组定位；兑现北极星指标 #2（调用链可信度可见性——llm 修复的边从"可辨认（视觉语言）+ 可查询（API 已通）"升级为"可解释（图视图里点边即读 LLM 推理）"，闭合"看到 ★ → 想知道为什么 → 翻 jsonl"的最后一段)+ #5（状态透明度——每条 llm 边的修复过程在图视图里直接可见，无需绕路日志文件）+ 候选优化方向 #1（审阅面板信息密度—调用上下文代码片段）+ #2（按 resolved_by 视觉语言—llm 档独占 inspector）；pytest 153/153 绿，`npm run build` 绿（4.81s）。
---

## Key references

- Architecture：[`docs/architecture.md`](docs/architecture.md) — 13 章 + 56 条 ADR
- E2E Plan：[`docs/e2e-test-plan.md`](docs/e2e-test-plan.md) — 7 场景 + CastEngine 基线
- Gap analysis ADRs：[`docs/adr/0001-gap-analysis-corrections.md`](docs/adr/0001-gap-analysis-corrections.md) · [`0002-gap-analysis-round2.md`](docs/adr/0002-gap-analysis-round2.md) · [`0003-gap-analysis-round3.md`](docs/adr/0003-gap-analysis-round3.md)
- Agent skills docs：[`docs/agents/`](docs/agents/) — issue-tracker / triage-labels / domain
- 复用的上游仓：AI4CParser（解析）· codewiki_lite（source 点 REST）· codemap（Neo4j 存储层原型）
