# opencode + GLM-5 + CastEngine 测试计划

> 基于 2026-05-13 首次 7-entry E2E 和 2026-05-15 3-entry E2E 的实测遥测数据。

## 测试环境

| 组件 | 版本/配置 |
|------|-----------|
| LLM 后端 | opencode CLI + GLM-5 (via DashScope) |
| 目标代码 | OpenHarmony CastEngine（3 模块，302 .cpp 文件） |
| Neo4j | 5.x (bolt://localhost:7687) |
| codewiki_lite | localhost:8400（或 fixture JSON） |
| opencode 参数 | `run --pure --dangerously-skip-permissions` |
| DashScope 凭证 | `~/.claude/settings-alibaba.json` |

## 实测遥测基线

### 7-entry run (2026-05-13)

| 指标 | 值 |
|------|-----|
| Source Points | 7 |
| RepairLog 总数 | 34 |
| LLM 边总数 | 35 |
| reasoning_summary 非空率 | 100% |
| 0-edge 放弃 source 数 | 3/7 (43%) |
| 门禁通过 source 数 | 0/7 (0%) |
| 最终状态 | 全部 partial_complete |

### 3-entry run (2026-05-15)

| 指标 | 值 |
|------|-----|
| Source Points | 3 (40de80074831, 94a77ea67585, 0644e8a39712) |
| RepairLog 总数 | 25 |
| LLM 边总数 | 27 |
| reasoning_summary 非空率 | 100% (25/25) |
| 门禁通过 source 数 | 0/3 (0%) |
| 最终状态 | 全部 partial_complete |
| 总耗时 | ~1077s (~18min) |

---

## 断言清单

### A1. Reasoning 捕获命中率

**定义**: `reasoning_summary` 非空的 RepairLog 占比。

| 断言 | 阈值 | 实测 |
|------|------|------|
| reasoning_summary 非空率 | ≥ 95% | 100% |
| reasoning_summary 平均长度 | ≥ 20 chars | ~80 chars |
| llm_response 非空率 | ≥ 95% | 100% |

**验证 Cypher**:
```cypher
MATCH (r:RepairLog)
WHERE r.reasoning_summary IS NOT NULL AND r.reasoning_summary <> ''
RETURN count(r) AS with_summary,
       count(r) * 100.0 / (MATCH (r2:RepairLog) RETURN count(r2)) AS pct
```

### A2. 放弃率按 Source Kind 分布

**定义**: agent 退出时 0 条新 LLM 边的 source 占比。

| Source Kind | 预期放弃率 | 实测 (7-entry) |
|-------------|-----------|----------------|
| IPC Stub (OnRemoteRequest) | 30-50% | ~43% (3/7) |
| Listener Callback | 20-40% | 待验证 |
| Virtual Dispatch | 10-30% | 待验证 |

**放弃原因分类**:
- `agent_exited_without_edge`: agent 正常退出但未写入任何边
- `subprocess_timeout`: 超时被 kill
- `subprocess_crash`: 非零退出码

**验证方法**:
```bash
# 查询每个 source 的 LLM 边数
codemap-lite status --config config.yaml
# 或直接查 Neo4j:
MATCH (sp:SourcePoint)
OPTIONAL MATCH (sp)-[:IS_SOURCE]->(f:Function)-[:CALLS*0..]->(g:Function)
OPTIONAL MATCH (g)-[c:CALLS {resolved_by: 'llm'}]->()
RETURN sp.id, count(DISTINCT c) AS llm_edges
```

### A3. Mirror 吞吐

**定义**: 单位时间内 agent 产出的 LLM 边数。

| 指标 | 值 |
|------|-----|
| 总 LLM 边 / 总耗时 | 27 edges / 1077s ≈ 0.025 edges/s |
| 每 source 平均耗时 | ~359s (~6min) |
| 每 source 平均 LLM 边 | ~9 edges |
| max_concurrency | 5 |

**基线断言**: 在 max_concurrency=5 下，3 个 source 应在 20min 内完成。

### A4. 门禁通过率按 Source Kind 分布

**定义**: `check-complete` 返回 `complete=True` 的 source 占比。

| Source Kind | 预期通过率 | 实测 |
|-------------|-----------|------|
| IPC Stub | 0-10% | 0% |
| 全部 | 0-20% | 0% (0/7, 0/3) |

**已知限制**: CastEngine 的 IPC Stub source 通常有 10-30 个 pending GAP，GLM-5 在 3 次重试内无法全部解决。门禁通过需要：
1. 更强的 LLM（如 Claude）
2. 更多重试次数
3. 更小粒度的 source（GAP 数 < 5）

### A5. 审计字段完整性

**定义**: 每次 gate 失败后 `last_attempt_timestamp` 和 `last_attempt_reason` 被正确写入。

| 断言 | 阈值 |
|------|------|
| 失败 source 的 pending GAP 有 `last_attempt_reason` | 100% |
| `last_attempt_reason` ∈ 已知枚举 | 100% |
| `last_attempt_timestamp` 为 ISO-8601 UTC | 100% |

**验证 Cypher**:
```cypher
MATCH (u:UnresolvedCall)
WHERE u.last_attempt_reason IS NOT NULL
RETURN u.last_attempt_reason, count(u) AS cnt
ORDER BY cnt DESC
```

---

## 已知问题

1. **35 LLM 边 vs 34 RepairLog (7-entry run)**: 1 条差值，疑似 `call_location` 路径格式不一致导致 RepairLog MERGE 覆盖。
2. **门禁从未 pass**: 所有 source 都有剩余 pending GAP，GLM-5 能力不足以在 3 次重试内解全。
3. **0-edge 放弃模式**: IPC Stub 类 source 的 GAP 多为跨模块虚函数派发，GLM-5 倾向于放弃而非猜测。

---

## 执行方法

```bash
# 完整 E2E（含修复）
python -m tests.run_e2e_full --config config.yaml --no-frontend

# 仅修复（指定 source 数量）
python -m tests.run_e2e_repair --sample 3 --timeout 1200

# 验证报告
cat tests/_e2e_integration_report.json | python -m json.tool
```

## 下一步

- [ ] 用 Claude (claudecode) 替代 GLM-5 跑同样 3 个 source，对比 LLM 边数和门禁通过率
- [ ] 构造一个 GAP 数 ≤ 3 的 source，验证门禁 `complete=True` 的真实路径
- [ ] 增加 source kind 多样性（当前 7 个全是 IPC Stub）
- [ ] 测量反例注入对第二轮修复的影响（A/B 对比）
