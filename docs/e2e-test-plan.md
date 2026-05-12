# CastEngine 端到端测试计划

> 使用 `C:\Task\openHarmony\foundation\CastEngine`（OpenHarmony 投屏引擎）作为 codemap-lite 的端到端验证目标。

## 测试环境前置条件

- Neo4j 5.x 运行中
- codewiki_lite 运行中（或 mock source 点 JSON）
- CastEngine 代码可访问
- claudecode / opencode CLI 已安装（Agent 修复测试需要）
- 默认修复后端 = **opencode + GLM-5 (via DashScope)**；凭证读自 `~/.claude/settings-alibaba.json`

## 目标代码概况

CastEngine 含三个模块，覆盖所有主要间接调用模式：

| 模块 | 核心间接调用模式 |
|------|----------------|
| castengine_cast_framework | 成员函数指针数组、Listener 回调、std::function |
| castengine_cast_plus_stream | 跨模块依赖、共享 session 实现 |
| castengine_wifi_display | 虚函数派发、工厂模式、IPC Proxy/Stub |

---

## E2E-1: 全量静态解析正确性

**目标**: 验证 tree-sitter 解析 + 直接调用构建 + GAP 标记的正确性。

| 验证点 | 预期结果 | CastEngine 验证位置 |
|--------|---------|-------------------|
| 函数提取 | 所有 .cpp/.h 中的函数被提取为 Function 节点 | `cast_session_impl.cpp` 中 ProcessSetUp/ProcessPlay 等 ~20 个函数 |
| 直接调用 | 直接函数调用生成 CALLS 边 (resolved_by=symbol_table) | `cast_session_impl.cpp:184` — `CastDeviceDataManager::GetInstance()` |
| 文件节点 | 每个源文件一个 File 节点，含 SHA256 hash | 三模块所有 .cpp/.h 文件 |
| 跨文件调用 | A.cpp 调用 B.h 中声明的函数 → 正确关联 | cast_framework 调用 common/ 中的工具函数 |
| DEFINES 关系 | File → Function 关系正确 | 每个 Function 有且仅有一个 DEFINES 入边 |

**验证方法**:
```bash
codemap-lite analyze --config config.yaml --target /path/to/CastEngine
# 然后查询 Neo4j:
# MATCH (f:Function) WHERE f.file_path CONTAINS 'cast_session_impl' RETURN f.name
# MATCH (a:Function)-[r:CALLS]->(b:Function) WHERE r.resolved_by='symbol_table' RETURN count(r)
```

---

## E2E-2: GAP 识别（间接调用检测）

**目标**: 验证 7 种间接调用模式都被正确识别为 UnresolvedCall。

### 模式 1: 成员函数指针数组
- **位置**: `castengine_cast_framework/service/src/session/src/cast_session_impl.cpp:990`
- **代码**: `(this->*stateProcessor_[msgId])(msg)`
- **预期 UnresolvedCall**:
  - call_type = "indirect"
  - call_expression = `(this->*stateProcessor_[msgId])(msg)`
  - candidates = [ProcessSetUp, ProcessSetUpSuccess, ProcessPlay, ProcessPause, ...]
  - 候选来源: `cast_session_impl_class.h:287-306` 数组初始化

### 模式 2: 虚函数派发
- **位置**: `castengine_wifi_display/services/impl/scene/wfd/wfd_sink_scene.cpp:47`
- **代码**: `scene->OnWifiAbilityResume()`
- **预期 UnresolvedCall**:
  - call_type = "virtual"
  - candidates = [WfdSinkScene::OnWifiAbilityResume]（CHA 分析结果）

### 模式 3: Listener 回调
- **位置**: `castengine_cast_framework/service/src/session/src/cast_session_impl.cpp:235`
- **代码**: `listener->OnDeviceState(DeviceStateInfo{...})`
- **预期 UnresolvedCall**:
  - call_type = "virtual"
  - candidates = [所有 ICastSessionListenerImpl 的实现类]

### 模式 4: 工厂模式
- **位置**: `castengine_wifi_display` 中 `WfdSinkFactory::CreateSink()` 返回值的后续方法调用
- **预期**: 工厂返回值类型不确定 → 后续方法调用标记为 GAP

### 模式 5: std::function
- **位置**: `cast_session_impl_class.h:254` — `std::function<void(int)> serviceCallback_`
- **调用位置**: serviceCallback_ 被调用处
- **预期 UnresolvedCall**: call_type = "indirect"

### 模式 6: IPC Proxy/Stub
- **位置**: `*_impl_proxy.cpp` 中通过 IPC 发送请求
- **预期**: 跨进程调用无法静态解析 → 标记为 GAP

### 模式 7: Singleton 返回值方法调用
- **位置**: `cast_session_impl.cpp:184` — `CastDeviceDataManager::GetInstance().SomeMethod()`
- **预期**: GetInstance() 本身是直接调用；但如果返回类型是接口/基类，后续方法调用可能是 GAP

**验证方法**:
```cypher
MATCH (u:UnresolvedCall) RETURN u.call_type, u.call_expression, u.caller_id, count(*)
ORDER BY u.call_type
```

---

## E2E-3: Agent 修复验证

**目标**: 验证 LLM Agent 能正确修复各类 GAP。

### 修复后端: opencode + GLM-5 (via DashScope)

生产/集成环境默认用 **opencode** 作为 Agent 前端，**GLM-5**（智谱，通过阿里云 DashScope 提供的 OpenAI 兼容网关）作为推理模型。`claude` / `claudecode` 仍作为备选后端保留。

**环境前置条件**:

1. `opencode` CLI 已安装（本地验证使用 v1.14.39）。
2. `~/.config/opencode/opencode.json` 配置 `dashscope` provider：
   ```json
   "dashscope": {
     "npm": "@ai-sdk/openai-compatible",
     "name": "DashScope (Aliyun)",
     "options": {
       "baseURL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
       "apiKey": "sk-..."
     },
     "models": {
       "glm-5": { "name": "GLM-5 (via DashScope)" }
     }
   }
   ```
3. `~/.claude/settings-alibaba.json` 保存同一对 `OPENAI_BASE_URL` / `OPENAI_API_KEY`，供脚本自动读取：
   ```json
   { "env": {
       "OPENAI_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
       "OPENAI_API_KEY": "sk-..."
   } }
   ```
4. **WSL / 代理注意事项**: 若系统设置了 `http_proxy` / `https_proxy` / `all_proxy`（常见 WSL 配置指向 `172.*:7897` 等主机代理），必须在执行 `opencode` 前 unset，否则 DashScope 出站 HTTPS 会被拦截。`tests/run_e2e_repair.py` 已在 subprocess 启动前自动剥离这些变量。

**RepairConfig 配置**（`codemap_lite.analysis.repair_orchestrator.RepairConfig`）:

```python
RepairConfig(
    target_dir=Path("/path/to/CastEngine"),
    backend="opencode",
    command="opencode",
    args=["run", "--pure", "-m", "dashscope/glm-5", "--dangerously-skip-permissions"],
    env={
        "OPENAI_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "OPENAI_API_KEY": "sk-...",
    },
    log_dir=Path("tests/_e2e_repair_logs"),
    max_concurrency=2,  # DashScope rate-limit friendly
)
```

`--pure` 绕过 opencode 的外部插件（否则会在无 TTY 的 subprocess 下挂起）；`--dangerously-skip-permissions` 跳过交互式工具许可提示。

**真实 E2E 脚本**: `tests/run_e2e_repair.py`

脚本按固定入口点（**per-entry-point**）驱动，而不是按 GAP 分桶抽样。每次运行的成功判据是：**schema 不变量全部保持** 且 **后端探针全绿**；Agent 修复结果逐条记录到 report，但不作为整体 success 门禁（反映 LLM 阶段的不确定性）。

#### 固定入口点

| 入口名 | 选取动机（覆盖的间接调用模式） |
|-------|-------------------------------|
| `CastSessionImpl::ProcessSetUp` | member_fn_ptr（`stateProcessor_` 数组派发） |
| `UnpackFuA` | 直接调用入口 + 下游 virtual 扩散 |
| `CastStreamManager::OnEvent` | virtual listener 扇出 |
| `WfdSessionImpl::ProcessCommand` | member_fn_ptr handler table |

入口名通过 `store._functions` 的 `(abs_file_posix, name)` + bare-name fallback 索引解析为 `FunctionNode.id`；候选需满足 `file_path` 位于 CastEngine 子树内，取 `start_line` 最小者作为定义点。

#### A→G 流水线阶段

| 阶段 | 动作 | 产物 |
|------|------|------|
| **A** Preflight | 校验 `opencode` / `node` / `npm` / CastEngine 根目录；从 `~/.claude/settings-alibaba.json` 读取 `OPENAI_BASE_URL` / `OPENAI_API_KEY`；剥离 `http_proxy` / `https_proxy` / `all_proxy`（含大写）以放行 DashScope 出站 | `check_environment` 信息 |
| **B** Static analysis | `PipelineOrchestrator.run_full_analysis()` 写入共享 `InMemoryGraphStore`（Phase 1-3：无悬挂边、bare-name 歧义 → UC、`resolved_by=symbol_table` 仅对 DIRECT） | `files_scanned` / `functions_found` / `direct_calls` / `unresolved_calls` |
| **C** Entry resolution | 固定入口名 → `FunctionNode.id`（CastEngine 子树内 `min(start_line)`） | `EntryPoint(name, function_id, file_path, start_line)` |
| **D** Repair loop | `E2ERepairHarness`（继承 `RepairOrchestrator`）：每个入口最多 **3 次尝试**，每次 inject `CLAUDE.md` + `.icslpreprocess/` → `opencode run --pure -m <model> --dangerously-skip-permissions` → 从 stdout 解析 JSON edges（fenced / bare / 单对象三种形态）→ `icsl_tools.write_edge` 落盘到 store → `icsl_tools.check_complete` 门禁。门禁失败时用 `_render_counter_examples` 生成残留 GAP 清单，作为反例累积到下一次注入的 CLAUDE.md | `SourceRepairResult(source_id, success, attempts, error?)` + `attempt{N}.log`（stdout/stderr/rc） |
| **E** Invariant scan | 遍历全部 `store._calls_edges` 与 `store._unresolved_calls`，断言 Phase 1-3 不变量（见下表） | `InvariantViolation(rule, detail)` 列表 |
| **F** Backend + Frontend probes | `uvicorn` 绑定 `_find_free_port()` 启后端线程（daemon）；探 `/health`、`/api/v1/stats`、`/api/v1/functions/{id}/call-chain?depth=3`（使用首个入口）；可选 `npm run dev` 启 Vite（`strictPort=true`，`:5173`），仅当 `frontend/node_modules` 已存在时执行 | `backend_probes` / `frontend_probes` |
| **G** Report | `RunReport` dataclass 序列化成 JSON，落盘到 `--report` 指定路径 | `tests/_e2e_repair_logs/report.json` |

#### 门禁语义（source-scoped completeness）

`_StoreAdapter.get_pending_gaps_for_source` 只返回 **caller 可达于入口点的** UnresolvedCall。可达集合在 harness 初始化时通过 `store.get_reachable_subgraph(source_id, max_depth=50)` 预计算并缓存为 `set[str]`，避免每次门禁重 BFS。`icsl_tools.check_complete` 按此视图判定 `complete`，与架构 §6 的 source 级闭合保持一致。

#### 不变量检查（`check_invariants`）

| 规则 | 说明 |
|------|------|
| `resolved_by_enum` | 每条 CALLS 边的 `resolved_by ∈ {symbol_table, signature, dataflow, context, llm}` |
| `no_dangling_edges` | caller 与 callee 必须都在 `store._functions` 中（icsl_tools 已保证，Phase 1 回归防护） |
| `symbol_table_direct_only` | `resolved_by == symbol_table` 必须 `call_type == direct`（Phase 3：间接派发绝不降级成 symbol_table） |
| `uc_call_type_nonempty` | 每个 UC 的 `call_type` 非空（Phase 3 UC surface 规则） |
| `uc_caller_known` | 每个 UC 的 `caller_id` 必须在 `store._functions` 中 |

任一规则违反都会进入 `RunReport.invariants`，且使 `RunReport.success = False`。

#### 运行命令

```bash
# 默认：4 个入口点 + 后端 + 前端 vite 探针
python -m tests.run_e2e_repair

# 跳过 vite（无 node/npm 环境，或只想验证后端）
python -m tests.run_e2e_repair --no-frontend

# 单入口冒烟
python -m tests.run_e2e_repair --entries UnpackFuA --timeout 180

# 调换模型或落盘路径
python -m tests.run_e2e_repair --model dashscope/glm-5 \
    --concurrency 2 \
    --report tests/_e2e_repair_logs/run-2026-05-12.json
```

CLI 参数速查：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--entries NAME [NAME...]` | 上表 4 个 | 入口点名（`Class::Method` 或裸函数名） |
| `--model SPEC` | `dashscope/glm-5` | 透传给 `opencode run -m` |
| `--timeout SEC` | `240` | 每个入口的 opencode 子进程超时；总超时 = `timeout × len(entries)` |
| `--no-frontend` | 关 | 跳过 Stage F 的 Vite 探针（A 阶段也不再强制 node/npm） |
| `--concurrency N` | `2` | 并发 repair 子进程数（DashScope 限流友好） |
| `--report PATH` | `tests/_e2e_repair_logs/report.json` | `RunReport` JSON 落盘路径 |

#### RepairConfig（脚本内部组装）

```python
repair_config = RepairConfig(
    target_dir=CASTENGINE_ROOT,
    backend="opencode",
    command="opencode",
    args=["run", "--pure", "-m", args.model, "--dangerously-skip-permissions"],
    max_concurrency=args.concurrency,
    env=llm_env,  # {OPENAI_BASE_URL, OPENAI_API_KEY}
    log_dir=LOG_ROOT,  # tests/_e2e_repair_logs/
)
```

`--pure` 绕过 opencode 的外部插件（否则会在无 TTY 的 subprocess 下挂起）；`--dangerously-skip-permissions` 跳过交互式工具许可提示。

#### 覆盖的间接调用场景

| 场景 | 入口点 | Agent 预期行为 |
|------|--------|----------------|
| 成员函数指针数组 | `CastSessionImpl::ProcessSetUp`（定义处），下游 `ProcessMessage` 的 `(this->*stateProcessor_[msgId])(msg)` | 读取 `cast_session_impl_class.h:287-306` 的数组初始化，识别非 `nullptr` 目标，每条目标产一条 LLM CALLS 边 |
| 虚函数派发 | `CastStreamManager::OnEvent` 下游的 `scene->OnWifiAbilityResume()` 等 | 枚举基类的全部实现类，产多条候选边 |
| Listener 回调 | `CastSessionImpl::ProcessSetUp` 下游 `listener->OnDeviceState(...)` | 列出 `ICastSessionListenerImpl` 全部实现，每个实现一条边 |
| Handler table | `WfdSessionImpl::ProcessCommand` | 与 member_fn_ptr 同形式，数组索引由命令号驱动 |

Agent 的 stdout 可以是三种 JSON 形态之一：

1. 围栏代码块 ` ```json [...] ``` `
2. 裸顶层数组 `[{...}, {...}]`
3. 单个对象（自动包装成单元素数组）

数组元素必须包含 `caller_id` / `callee_id` / `call_type` / `call_file` / `call_line`；缺字段或 `call_line` 非整数的条目在 `_apply_agent_edges` 中计入 `skipped`，不阻断修复流程。

#### RunReport 字段

```json
{
  "started_at": 1715500000.0,
  "static_stats": { "files_scanned": 716, "functions_found": 5491, ... },
  "entries": [{ "name": "CastSessionImpl::ProcessSetUp", "function_id": "...", ... }],
  "repairs": [{ "source_id": "...", "success": true, "attempts": 1, "error": null }],
  "invariants": [],
  "backend_probes": { "base_url": "http://127.0.0.1:XXXX", "endpoints": { "/health": {...}, ... }, "ok": true },
  "frontend_probes": { "ok": true, "port": 5173, "endpoints": {...} } | { "skipped": true, "reason": "..." },
  "success": true,
  "duration_s": 312.7
}
```

#### Success 判据

```
success  ≡  (len(invariants) == 0)  ∧  backend_probes.ok
```

Agent 修复是否成功逐条记录在 `repairs[].success`，但**不**进入总 success——LLM 阶段的不稳定性不应把整条 pipeline 判失败。回归关注点在于：静态阶段无漂移 + 后端 API 契约可达，这是前端可用的最小集。

---

## E2E-4: 增量更新验证

**目标**: 文件变更后，仅受影响子图被重建。

**步骤**:
1. 全量解析 CastEngine → 记录 Neo4j 中 Function 节点数和 CALLS 边数
2. 修改 `cast_session_impl.cpp`（在某函数中添加一个新的直接调用）
3. 运行 `codemap-lite analyze --incremental --config config.yaml`
4. **验证**:
   - `cast_session_impl.cpp` 的 Function 节点被重建（body_hash 变化）
   - 其他文件的 Function 节点不变（body_hash 不变）
   - 新的直接调用边被添加
   - 指向该文件函数的 LLM 修复边被失效 → 新 UnresolvedCall 生成
   - 不相关模块（如 wifi_display）完全不受影响

**验证查询**:
```cypher
-- 变更前后对比
MATCH (f:Function) WHERE f.file_path CONTAINS 'cast_session_impl'
RETURN f.name, f.body_hash
-- 确认其他文件不变
MATCH (f:Function) WHERE f.file_path CONTAINS 'wfd_sink_scene'
RETURN f.body_hash  -- 应与变更前相同
```

---

## E2E-5: 审阅 + 反例反馈循环

**目标**: 验证人工审阅 → 反例生成 → Agent 重新修复的完整循环。

**步骤**:
1. Agent 修复了 `stateProcessor_` 的某个 GAP → 假设写入了一条错误的 CALLS 边（指向了错误的目标函数）
2. 通过 API 标记该边为错误，填写正确目标:
   ```bash
   curl -X POST /api/v1/reviews -d '{
     "caller_id": "...",
     "callee_id": "...(错误目标)",
     "correct_target": "...(正确目标)",
     "verdict": "incorrect"
   }'
   ```
3. **验证**:
   - 错误 CALLS 边被删除
   - 对应 RepairLog 被删除
   - 新 UnresolvedCall 被创建（retry_count=0）
   - `counter_examples.md` 更新（包含泛化后的反例模式）
   - 重新触发 Agent 修复 → 这次 CLAUDE.md 引用了更新后的反例库
   - Agent 这次修复正确（验证新 CALLS 边指向正确目标）

---

## E2E-6: 跨模块调用追踪

**目标**: 验证 Agent 能追踪跨模块的调用链。

**场景**:
1. Source 点在 `castengine_cast_framework` 中
2. 调用链: cast_framework → cast_plus_stream（共享 session 实现）
3. cast_plus_stream 中存在间接调用 GAP

**验证**:
- Agent 的 BFS 遍历能跨越模块边界
- 跨模块 CALLS 边正确建立
- `query-reachable` 返回的子图包含两个模块的函数

---

## E2E-7: 门禁 + 重试机制

**目标**: 验证 Orchestrator 的门禁验证和重试逻辑。

**步骤**:
1. 配置一个 source 点，其可达路径上有多个 GAP
2. 模拟 Agent 只修复了部分 GAP（通过限制 Agent 的 context 或使用 mock）
3. **验证**:
   - `check-complete --source <id>` 返回有残留 pending GAP
   - Orchestrator 重启 Agent（新 subprocess，日志中可见）
   - 已修复的 GAP 被跳过（Agent 检查边已存在）
   - 3 次重试后仍失败的 GAP → `status="unresolvable"`
   - SourcePoint.status = "partial_complete"

**验证查询**:
```cypher
MATCH (s:SourcePoint {id: '<source_id>'})
RETURN s.status  -- 'partial_complete'

MATCH (u:UnresolvedCall {status: 'unresolvable'})
WHERE u.caller_id IN [可达函数列表]
RETURN u.retry_count  -- 应为 3
```

---

## 测试分层策略

| 层级 | 工具 | 覆盖范围 | 运行频率 |
|------|------|---------|---------|
| 单元测试 | pytest | 每个模块的核心逻辑 | 每次提交 |
| 集成测试 | pytest + Neo4j testcontainer | 存储层 CRUD + 查询 | 每次提交 |
| E2E（确定性） | pytest + mock subprocess | E2E-1, E2E-2, E2E-4, E2E-7 | 每次提交 |
| E2E（LLM） | pytest @slow | E2E-3, E2E-5, E2E-6 | 手动/CI nightly |
| 前端 E2E | Cypress | 审阅交互流程 | 每次前端变更 |

## 性能基线

| 指标 | 目标 | 测量方法 |
|------|------|---------|
| CastEngine 全量解析 | < 5 分钟 | `time codemap-lite analyze` |
| 增量解析（单文件变更） | < 30 秒 | `time codemap-lite analyze --incremental` |
| 单个 source 点修复 | < 10 分钟 | Agent subprocess 运行时间 |
| API 响应（图浏览） | < 500ms | `curl` 计时 |
