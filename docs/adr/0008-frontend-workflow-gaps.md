# ADR-0008: 前端操作动线 GAP 修复 — Agent 实时日志 + 反例 CRUD

## Status

Accepted (2026-05-16)

## Context

从用户操作动线（获取source点 → 查看 → 修复 → 结果 → 失败 → 日志 → 效果 → 反例库）审视前端，发现以下阻塞性问题：

1. **修复进度不可见**：点击 Repair 后无即时反馈，用户不知道 agent 是否启动
2. **Agent 推理过程不可见**：修复进行中只显示 "agent running…"，无法看到 agent 的实时思考
3. **页面间无联动**：SourcePointList 和 RepairActivity 各自展示 repair logs，无法从 source 跳转到对应日志
4. **反例库只能添加不能管理**：错误的反例无法删除或修正
5. **失败 GAP 无重试入口**：看到失败后只能重新修复整个 source

## Decision

### 1. Source 卡片内嵌终端框

在 SourcePointList 的 SourceDetail 组件中，当 source 处于 running/gate_checking 状态时，内嵌一个暗色终端框（类似 terminal），实时显示 agent 的最新 20-30 行输出。

数据来源：后端新增 `GET /api/v1/repair-logs/live?source_id=X&tail=30` 端点，读取 `logs/repair/<source_id>/attempt_N.log` 文件尾部。前端每 2-3s 轮询。

修复完成后终端框变为静态，底部显示"查看完整推理日志"链接跳转到 `/repairs?source=<id>`。

### 2. Live tail API

```
GET /api/v1/repair-logs/live?source_id={id}&tail={n}
Response: {lines: string[], attempt: number, finished: boolean, source_id: string}
```

- 扫描 `logs/repair/<safe_dirname(source_id)>/attempt_*.log`，取最新文件
- 读尾部 N 行（默认 30）
- `finished` = progress.json 中 state ∈ {succeeded, failed}

### 3. 反例 CRUD

```
DELETE /api/v1/feedback/{id}  → 删除反例
PUT /api/v1/feedback/{id}    → 编辑 pattern/context/correct_target
```

FeedbackStore 新增 `delete(id)` 和 `update(id, fields)` 方法。

### 4. 修复触发即时反馈

点击 Repair 后：
- 本地状态立即设为 running
- 自动展开该 source 的详情区域
- 终端框开始轮询日志

### 5. 重试粒度

保持 source 级别重试（复用 `triggerRepair([source_id])`）。当前 agent 是 source 级别启动的 subprocess，单 GAP 重试需要改 agent 合约，暂不实施。

## Consequences

- 前端新增 ~200 行代码（终端框组件 + 轮询逻辑）
- 后端新增 1 个 GET 端点 + 2 个 CRUD 端点
- 日志文件读取引入文件 I/O，但 tail 操作轻量（seek 到文件末尾）
- 轮询间隔 2-3s 可接受（不需要 WebSocket/SSE 的复杂度）
- 反例 CRUD 需要 FeedbackStore 支持 update/delete，当前是 append-only JSON 文件

## References

- architecture.md §3 进度通信机制
- architecture.md §3 反馈机制
- ADR-0007 前端视觉系统
- CLAUDE.md 前端持续优化 北极星指标 #4 状态透明度
