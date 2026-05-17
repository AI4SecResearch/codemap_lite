# ADR-0007: 前端视觉系统 — 共享组件库 + 全局品质提升

## 状态

已接受（2026-05-16）

## 背景

ADR-0006 解决了操作动线问题（删除 ReviewQueue，SourcePointList 成为操作主控台），但视觉品质仍有不足：

- 无统一组件库，按钮/徽章/卡片样式分散在各页面内联
- 无图标系统，纯文字按钮缺乏辨识度
- 加载态用 "Loading…" 文本，无骨架屏
- 无确认对话框，危险操作（mark-wrong）无二次确认
- 无搜索/过滤，大列表效率低
- 无键盘快捷键，审阅效率受限
- 字体/阴影/圆角/过渡无统一规范

## 决策

### 1. 引入 lucide-react 图标库

轻量（tree-shakeable）、风格统一、与 Tailwind 配合良好。

### 2. 建立 `components/ui/` 共享组件库

| 组件 | 职责 |
|------|------|
| Button | primary/secondary/ghost/danger 四态 + loading spinner |
| Badge | 状态徽章（tone 色系 + 可选图标） |
| Card | 卡片容器（hover 阴影提升、可点击态） |
| ProgressBar | 动画进度条（渐变色 + 百分比标签） |
| SearchInput | 搜索框（图标 + 清除按钮 + debounce） |
| Skeleton | 骨架屏加载态 |
| ConfirmDialog | 确认对话框（mark-wrong 等危险操作） |
| EmptyState | 空状态（图标 + 说明 + CTA 按钮） |
| Timestamp | 相对时间显示（"3 分钟前"） |

### 3. 全局视觉规范

- **字体**：Inter（Google Fonts CDN）
- **阴影层级**：shadow-sm → shadow-md → shadow-lg 三级
- **圆角**：卡片 rounded-xl，按钮 rounded-lg，徽章 rounded-full
- **过渡**：所有交互元素 transition-all duration-200
- **焦点**：focus-visible:ring-2 ring-blue-500 ring-offset-2
- **数字**：tabular-nums 等宽数字

### 4. SourcePointList 增强

- 批量选择（checkbox + 底部浮动操作栏）
- 键盘快捷键（j/k 导航、Enter 展开、r 修复、Space 选中）
- 搜索框（按 signature/file 过滤）
- 确认对话框（mark-wrong 前弹出）

### 5. Dashboard 修复效果趋势

- 解析率、本轮新增边、反例命中三卡片
- StatCard 升级（图标 + 渐变背景 + 趋势箭头）

### 6. FeedbackLog 美化

- 左侧彩色边条（按 pattern 类型着色）
- 高亮动画（scrollIntoView + fade-in）
- 空状态引导

## 替代方案（被否决）

| 方案 | 否决原因 |
|------|----------|
| 引入完整 UI 框架（shadcn/ui） | 过重，项目规模不需要 |
| 保持纯 Tailwind 内联 | 样式分散，无法保证一致性 |
| Material UI | 风格不匹配，bundle 过大 |

## 后果

### 正面
- 视觉一致性：所有页面共享同一套组件和设计语言
- 开发效率：新页面/功能直接复用组件库
- 审阅效率：键盘快捷键 + 搜索 + 批量操作减少重复点击
- 品质感：现代 UI 语言提升专业度

### 负面
- 新增 lucide-react 依赖（~15KB gzipped，tree-shakeable）
- 组件库需要维护
- 首次加载略增（Inter 字体 CDN）

## 对齐

- architecture.md §5 前端设计（视觉品质要求）
- 北极星指标 #1 单个 GAP 审阅耗时（键盘快捷键 + 批量操作）
- 北极星指标 #4 状态透明度（骨架屏 + 进度条 + 空状态）
- ADR-0006 操作动线重设计（本 ADR 在其基础上提升视觉层）
