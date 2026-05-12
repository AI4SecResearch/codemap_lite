# ADR 0004 — progress.json Schema Correction

Date: 2026-05-13
Status: Accepted (supersedes ADR 0001 H5 partial + implementation-plan.md §2.4 schema reference)

## Context

架构设计阶段，Repair Agent Notification hook 写出的进度文件 (`logs/repair/{source_id}/progress.json`) 采用的 schema 在两份早期文档里登记为：

```json
{
  "fixed_gaps": 3,
  "total_gaps": 10,
  "current_gap_id": "gap_002"
}
```

参见 `docs/adr/0001-gap-analysis-corrections.md` H5 与 `docs/implementation-plan.md §2.4`。`codemap_lite/agent/hooks/log_notification.py` 初版按此 schema 实现（2026-05-12 初次落地）。

实际落地时，面向前端的 `/api/v1/analyze/status` 端点（`codemap_lite/api/routes/analyze.py`）在聚合时选择了另一套键名：

```json
{
  "source_id": "src_001",
  "gaps_fixed": 3,
  "gaps_total": 10,
  "current_gap": "gap_002"
}
```

这套键名后来一路固化到：

- `frontend/src/api/client.ts` 的 `SourceProgress` interface
- `frontend/src/pages/Dashboard.tsx` 的 Repair Progress 卡
- `frontend/src/pages/SourcePointList.tsx` 的 Progress 列（Tick #24 / Loop Note 2026-05-13）
- `codemap_lite/cli.py status` 的读取逻辑
- `tests/test_api.py` 的 fixtures

导致 hook 侧输出与所有消费者之间存在字段名不匹配（`fixed_gaps` vs `gaps_fixed` / `total_gaps` vs `gaps_total` / `current_gap_id` vs `current_gap`）。在实际运行中，`/api/v1/analyze/status` 的 `data.get("gaps_fixed", 0)` 会始终读到 0，前端进度列/卡永远渲染 `—` 或零态，导致 Tick #24 的 SourcePointList 进度列与 Dashboard 的 Repair Progress 在生产环境下完全失效。

`docs/architecture.md §3` 中仅用自然语言描述进度文件承载的字段语义，未锁定具体 JSON key，这也是两套 schema 可以并行存在而未被发现的根因。

## Decision

- **Canonical schema 以面向前端的 REST 契约为准**：`gaps_fixed` / `gaps_total` / `current_gap`。
- **Hook 对齐**：`log_notification.py` 输出采用 canonical schema，同时兼容读取旧 event 键（`fixed_gaps` / `total_gaps` / `current_gap_id`）以容忍 agent runtime 仍按旧文档发送 notification 事件的情形——即输入双读、输出单写。
- **架构文档固化**：在 `docs/architecture.md §3` 显式登记 progress.json 的 JSON schema，后续修改需走 ADR。
- **历史文档标记 superseded**：`docs/adr/0001-gap-analysis-corrections.md` H5 与 `docs/implementation-plan.md §2.4` 保留原文作为历史，但在原位置加 ADR 0004 指针。

## Rationale

1. **Consumer count**：canonical schema 已在 5 处消费方（API route、TS client、Dashboard、SourcePointList、CLI status）+ 1 个测试套件里固化。反向调整会触及前端 + 后端 + 测试多处；顺向调整只需改 1 个 hook 文件 + 1 个测试 + 2 份文档。
2. **外部性**：REST API 与前端 TS interface 是外部可见契约，hook 输出是进程内契约。修正方向让内部向外部看齐，符合"外契约更贵"的原则。
3. **向后兼容**：hook 双读旧事件键，让可能按旧文档模版实现的 agent notification 事件仍能被正确翻译，避免引入回归。

## Consequences

- `progress.json` 文件 schema 从此锁死为 `{gaps_fixed, gaps_total, current_gap}`；任何未来 agent backend 的 hook 实现都必须按此 key 输出。
- Tick #24 的 SourcePointList Progress 列与 Dashboard Repair Progress 卡从"看似工作、实则永远空态"升级为真实可用。
- ADR 0001 H5 的旧 schema 条目保留作为历史记录，但显式标注被 ADR 0004 取代。
- 无数据迁移负担：旧 progress.json 文件是瞬态产物（`.icslpreprocess/` 目录每轮 repair 清理，`logs/repair/` 每次运行新鲜写入），无需回溯。

## References

- `docs/architecture.md §3`（Repair Agent 进度文件契约，本 ADR 落地后固化 schema）
- `docs/adr/0001-gap-analysis-corrections.md` H5（旧 schema 的登记位置，被本 ADR 部分取代）
- `docs/implementation-plan.md §2.4`（旧 schema 的另一登记位置，同样被本 ADR 取代）
- `codemap_lite/agent/hooks/log_notification.py`（hook 对齐后的新实现）
- `codemap_lite/api/routes/analyze.py`（canonical 消费方之一）
- `frontend/src/api/client.ts` 的 `SourceProgress` interface（canonical 消费方之二）
