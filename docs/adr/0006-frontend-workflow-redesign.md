# ADR-0006: 前端操作动线重设计 — 删除 ReviewQueue，SourcePointList 成为操作主控台

## 状态

已接受（2026-05-15）

## 背景

当前前端 6 个页面（Dashboard / SourcePointList / FunctionBrowser / CallGraphView / ReviewQueue / FeedbackLog）各自独立，用户操作动线断裂：

- 不知道从哪里获取 source 点
- 不知道如何触发修复
- 修复结果、失败项、修复日志分散在不同页面
- ReviewQueue 信息太少，看不到 GAP 点、LLM 修复结果、LLM 推理过程

用户期望的操作动线：
```
获取 source 点 → 查看 source 点 → 选择修复 → 查看修复结果 → 查看失败项 → 查看修复日志/推理 → 总体效果 → 反例库
```

## 决策

1. **删除 ReviewQueue 页面**：所有审阅操作移入 SourcePointList 展开行
2. **SourcePointList 成为操作主控台**：承载获取/修复/审阅全动线
3. **WorkflowStepper 简化为 2 步**：Sources → Feedback
4. **每条 LLM 边审阅卡片展示代码上下文**：调用点代码（±3行）+ callee 完整函数体（可折叠）+ reasoning_summary
5. **新增 source-code API**：`GET /api/v1/source-code?file=&start=&end=` 支持前端读取目标代码片段
6. **retry_count=3 自动标记 unresolvable**：不需要手动 mark-unresolvable 操作

## 替代方案（被否决）

| 方案 | 否决原因 |
|------|----------|
| 保留 ReviewQueue + 新增 LLM Edges tab | 信息仍然分散，用户需要在多个页面间跳转 |
| ReviewQueue 专注逐条审阅 LLM 边 | 与 SourcePointList 展开行功能重复 |
| 三步 stepper（Sources → Review → Feedback） | Review 页面被删除，不再需要 |

## 后果

### 正面
- 用户操作动线连贯：一个页面完成获取→修复→审阅全流程
- 审阅者有足够代码上下文判断 LLM 边正确性
- 减少页面跳转，降低认知负担

### 负面
- SourcePointList 页面复杂度增加（展开行包含多层信息）
- 需要新增 source-code API 端点
- 跨页面 drill-down 契约全部从 `/review?...` 改为 `/sources?...`

### 影响范围
- `docs/architecture.md` §5 前端设计：重写
- `docs/architecture.md` §8 REST API：新增 source-code 端点
- `frontend/src/pages/ReviewQueue.tsx`：删除
- `frontend/src/pages/SourcePointList.tsx`：重写
- `frontend/src/App.tsx`：删除 Review 路由
- `frontend/src/components/WorkflowStepper.tsx`：2 步
- `frontend/src/pages/Dashboard.tsx`：drill-down 链接更新
- `codemap_lite/api/routes/graph.py`：新增 source-code 端点

## 对齐

- architecture.md §5 前端设计（本 ADR 驱动的重写）
- architecture.md §3 Retry 审计字段（retry_count=3 自动 unresolvable）
- architecture.md §8 REST API（新增 source-code 端点）
