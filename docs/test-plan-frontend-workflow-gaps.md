# 前端操作动线 GAP 修复 — 测试计划

## 对应 ADR

ADR-0008: 前端操作动线 GAP 修复 — Agent 实时日志 + 反例 CRUD

## 测试范围

覆盖 ADR-0008 中 12 项 GAP 修复的验证，分为后端单元测试、前端构建验证、端到端功能验证三层。

---

## 1. 后端单元测试（pytest）

### T1: Live tail API

**文件**: `tests/test_api.py` 或新建 `tests/test_repair_logs_live.py`

| 用例 | 断言 |
|------|------|
| `GET /repair-logs/live?source_id=abc&tail=30` 无日志文件 | 返回 `{lines: [], attempt: 0, finished: false}` |
| 写入 `logs/repair/abc/attempt_1.log` 50 行后请求 tail=30 | 返回最后 30 行，`attempt=1` |
| 多个 attempt 文件存在时取最新 | `attempt_3.log` 存在时返回其内容 |
| progress.json state=succeeded 时 | `finished=true` |
| progress.json state=running 时 | `finished=false` |
| source_id 含特殊字符（`/`、`::`） | 正确映射到 safe_dirname |
| tail=0 或 tail>200 | 返回 400 参数校验错误 |

### T2: Feedback DELETE

| 用例 | 断言 |
|------|------|
| `DELETE /feedback/{id}` 存在的反例 | 返回 200，再 GET 列表不含该 id |
| `DELETE /feedback/{id}` 不存在的 id | 返回 404 |

### T3: Feedback PUT

| 用例 | 断言 |
|------|------|
| `PUT /feedback/{id}` 更新 pattern | 返回 200，GET 该 id 返回新 pattern |
| `PUT /feedback/{id}` 更新 correct_target | 返回 200，字段已更新 |
| `PUT /feedback/{id}` 不存在的 id | 返回 404 |
| `PUT /feedback/{id}` 空 body | 返回 422 |

### T4: Source points ID 映射

| 用例 | 断言 |
|------|------|
| codewiki_lite 长路径 ID 的 source point | 返回的 `function_id` 是 12-char hex（Neo4j ID） |
| 多个同名函数时按文件路径消歧 | 返回正确的 Neo4j function ID |
| 无匹配函数时 | `function_id` 保持原值（降级） |

---

## 2. 前端构建验证

```bash
cd frontend && npm run build
```

必须零 TypeScript 错误、零 warning。新增的 API 方法类型签名必须与后端响应一致。

---

## 3. 端到端功能验证（人工 + npm run dev）

### E2E-F1: 修复触发 → 实时日志可见

**前置条件**: codewiki_lite 运行中，Neo4j 有数据，后端 `codemap-lite serve` 运行中

| 步骤 | 预期 |
|------|------|
| 1. 打开 /sources 页面 | 看到 source 点列表，GAP 数量 > 0 |
| 2. 展开一个 source 点 | 看到修复结果/未修复 GAP（如果有数据则默认展开） |
| 3. 点击该 source 的 "Repair" 按钮 | 卡片状态立即变为 running，详情区域自动展开 |
| 4. 观察终端框 | 暗色终端框出现，顶部显示 "Agent running…" + 绿色脉冲点 |
| 5. 等待 2-3s | 终端框内容更新，显示 agent 最新输出 |
| 6. 等待修复完成 | 终端框停止更新，底部出现"查看完整推理日志"链接 |
| 7. 点击链接 | 跳转到 /repairs?source=<id>，只显示该 source 的日志 |

### E2E-F2: Repair All 确认

| 步骤 | 预期 |
|------|------|
| 1. 点击 "Repair All" | 弹出确认对话框，显示 source 数量 |
| 2. 点击取消 | 对话框关闭，无操作 |
| 3. 再次点击 "Repair All" → 确认 | 修复开始，所有 source 状态变为 running |

### E2E-F3: 失败重试

| 步骤 | 预期 |
|------|------|
| 1. 展开一个有失败 GAP 的 source | 看到 Unresolved GAPs 区域 |
| 2. 看到失败分类摘要 | 显示 "X gate_failed · Y agent_error · Z timeout" |
| 3. 点击 "重试此 Source" | 触发修复，终端框出现 |

### E2E-F4: RepairActivity 过滤

| 步骤 | 预期 |
|------|------|
| 1. 打开 /repairs | 看到所有 repair logs |
| 2. 从 URL 添加 ?source=<id> | 只显示该 source 的 logs |
| 3. 使用筛选下拉选择 source | URL 更新，列表过滤 |

### E2E-F5: 反例库管理

| 步骤 | 预期 |
|------|------|
| 1. 打开 /feedback | 看到反例列表 |
| 2. 在搜索框输入关键词 | 列表实时过滤 |
| 3. 点击某反例的"编辑" | 字段变为可编辑，修改后保存 |
| 4. 点击某反例的"删除" | 弹出确认，确认后反例消失 |

### E2E-F6: 状态 tooltip

| 步骤 | 预期 |
|------|------|
| 1. Hover "pending" Badge | 显示 "等待修复 — agent 尚未处理此 GAP" |
| 2. Hover "unresolvable" Badge | 显示 "已放弃 — 重试 3 次后 agent 仍无法解决" |

### E2E-F7: 模块分组

| 步骤 | 预期 |
|------|------|
| 1. 切换到"按模块分组"视图 | source 按 module 字段分组显示 |
| 2. 每组标题显示模块名 + 数量 | 如 "castengine_cast_plus_stream_mirror (2)" |
| 3. 折叠/展开组 | 组内 source 隐藏/显示 |

### E2E-F8: 历史趋势

| 步骤 | 预期 |
|------|------|
| 1. 打开 Dashboard | 看到 Repair Effectiveness 区域 |
| 2. 下方显示趋势 | "今天 +N edges · 昨天 +M edges" |

---

## 4. 回归验证

每次改动后必须通过：
```bash
python -m pytest tests/ -x -q   # 后端全绿
cd frontend && npm run build     # 前端零错误
```

---

## 5. 验收标准

- [ ] 所有 T1-T4 单元测试通过
- [ ] 前端 build 零错误
- [ ] E2E-F1 修复触发后 3s 内终端框显示 agent 输出
- [ ] E2E-F2 Repair All 有确认对话框
- [ ] E2E-F3 失败 GAP 可重试
- [ ] E2E-F4 RepairActivity 支持 source 过滤
- [ ] E2E-F5 反例可搜索/删除/编辑
- [ ] E2E-F6 状态 Badge 有 tooltip
- [ ] E2E-F7 模块分组视图可用
- [ ] E2E-F8 Dashboard 显示趋势
