import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, Review, UnresolvedCall } from '../api/client';

type Tab = 'gaps' | 'reviews';
// architecture.md §3: UnresolvedCall.status ∈ {pending, unresolvable}
// (after retry_count ≥ 3 → unresolvable). The filter lets a reviewer
// zero in on GAPs the agent gave up on so the human review budget is
// spent where it matters most.
type StatusFilter = 'all' | 'pending' | 'unresolvable';

const STATUS_FILTERS: readonly StatusFilter[] = ['all', 'pending', 'unresolvable'];

function isStatusFilter(v: string | null): v is StatusFilter {
  return v !== null && (STATUS_FILTERS as readonly string[]).includes(v);
}

// architecture.md §3 Retry 审计字段 locks 4 categories — reviewers
// triaging ops (LLM stall / CLI misconfig) vs agent logic failures
// should be able to isolate one bucket instead of eye-scanning the
// 4 colored chips in GapDetail. Mirrors the ?status= contract.
type CategoryFilter =
  | 'all'
  | 'gate_failed'
  | 'agent_error'
  | 'subprocess_crash'
  | 'subprocess_timeout';

const CATEGORY_FILTERS: readonly CategoryFilter[] = [
  'all',
  'gate_failed',
  'agent_error',
  'subprocess_crash',
  'subprocess_timeout',
];

function isCategoryFilter(v: string | null): v is CategoryFilter {
  return v !== null && (CATEGORY_FILTERS as readonly string[]).includes(v);
}

// Extract the <category> prefix from a last_attempt_reason (format
// `<category>: <summary>`, architecture.md §3). Returns null if
// the reason is missing / malformed so "no audit stamp yet" GAPs
// aren't miscategorized into any bucket.
function extractCategory(reason: string | null | undefined): string | null {
  if (!reason) return null;
  const idx = reason.indexOf(':');
  if (idx < 0) return null;
  const prefix = reason.slice(0, idx).trim();
  return prefix.length > 0 ? prefix : null;
}

// Transient banner summarizing the last counter-example submission so
// the reviewer can tell at a glance whether their pattern opened a new
// rule or merged into an existing one — architecture.md §3 反馈机制
// steps 3-5 "相似 → 总结合并 / 不相似 → 新增"; 北极星指标 #5.
type FeedbackOutcome = {
  deduplicated: boolean;
  total: number;
  pattern: string;
};

function shorten(path: string): string {
  const parts = path.split('/');
  return parts.slice(-3).join('/');
}

// Shared status/retry chips used inline in each row and inside
// <GapDetail>. Architecture.md §3 UnresolvedCall lifecycle:
// retry_count ≥ 3 → status="unresolvable" → needs human attention.
function GapStatusChips({
  gap,
  size = 'sm',
}: {
  gap: UnresolvedCall;
  size?: 'sm' | 'md';
}) {
  const chips: { label: string; value: string; tone: string }[] = [];
  if (gap.status) {
    const tone =
      gap.status === 'unresolvable'
        ? 'bg-red-100 text-red-700 ring-1 ring-red-300'
        : gap.status === 'resolved'
        ? 'bg-green-100 text-green-700'
        : gap.status === 'failed'
        ? 'bg-red-100 text-red-700'
        : 'bg-gray-100 text-gray-700';
    chips.push({ label: 'status', value: gap.status, tone });
  }
  if (typeof gap.retry_count === 'number') {
    const tone =
      gap.retry_count >= 3
        ? 'bg-red-100 text-red-700'
        : gap.retry_count > 0
        ? 'bg-amber-100 text-amber-700'
        : 'bg-gray-100 text-gray-600';
    chips.push({
      label: 'retries',
      value: `${gap.retry_count}/3`,
      tone,
    });
  }
  if (chips.length === 0) return null;
  const px = size === 'sm' ? 'px-1.5 py-0' : 'px-2 py-0.5';
  return (
    <div className="flex flex-wrap gap-1 text-[11px]">
      {chips.map((c) => (
        <span
          key={`${c.label}:${c.value}`}
          className={`inline-flex items-center gap-1 rounded ${px} ${c.tone}`}
        >
          <span className="text-[9px] uppercase tracking-wide opacity-70">{c.label}</span>
          <span className="font-mono">{c.value}</span>
        </span>
      ))}
    </div>
  );
}

export default function ReviewQueue() {
  // architecture.md §5 跨页面 drill-down 契约：`?status=` + `?caller=`
  // 两个可选 query param 都作为对应筛选器的初值，也在用户切换/清除时
  // 回写到 URL，让 Dashboard StatCard、nav chip、FunctionBrowser GAP
  // chip 的链接能深链到预筛选列表（北极星 #1 & #5）。
  const [searchParams, setSearchParams] = useSearchParams();
  const initialStatus: StatusFilter = (() => {
    const raw = searchParams.get('status');
    return isStatusFilter(raw) ? raw : 'all';
  })();
  const initialCategory: CategoryFilter = (() => {
    const raw = searchParams.get('category');
    return isCategoryFilter(raw) ? raw : 'all';
  })();
  const initialCaller: string | null = searchParams.get('caller');

  const [tab, setTab] = useState<Tab>('gaps');
  const [gaps, setGaps] = useState<UnresolvedCall[]>([]);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>(initialStatus);
  const [categoryFilter, setCategoryFilter] = useState<CategoryFilter>(initialCategory);
  const [callerFilter, setCallerFilter] = useState<string | null>(initialCaller);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  // When non-null, the "Mark wrong" modal is open for this gap — the user
  // fills the correct target + generalized pattern that will be POSTed to
  // /api/v1/feedback (architecture.md §5 审阅标记错误时).
  const [wrongFor, setWrongFor] = useState<UnresolvedCall | null>(null);
  const [lastFeedback, setLastFeedback] = useState<FeedbackOutcome | null>(null);
  const tableRef = useRef<HTMLDivElement | null>(null);

  // Keep `?status=` in the URL in sync with the current filter, so
  // refresh / bookmark / share all preserve the drill-down state.
  useEffect(() => {
    const current = searchParams.get('status');
    if (statusFilter === 'all') {
      if (current !== null) {
        const next = new URLSearchParams(searchParams);
        next.delete('status');
        setSearchParams(next, { replace: true });
      }
    } else if (current !== statusFilter) {
      const next = new URLSearchParams(searchParams);
      next.set('status', statusFilter);
      setSearchParams(next, { replace: true });
    }
  }, [statusFilter, searchParams, setSearchParams]);

  // Mirror sync for `?caller=` — FunctionBrowser GAP chips deep-link
  // into a pre-filtered caller view; clearing the chip strips the param
  // so bookmarks/share behave like the status filter.
  useEffect(() => {
    const current = searchParams.get('caller');
    if (callerFilter === null || callerFilter === '') {
      if (current !== null) {
        const next = new URLSearchParams(searchParams);
        next.delete('caller');
        setSearchParams(next, { replace: true });
      }
    } else if (current !== callerFilter) {
      const next = new URLSearchParams(searchParams);
      next.set('caller', callerFilter);
      setSearchParams(next, { replace: true });
    }
  }, [callerFilter, searchParams, setSearchParams]);

  // Mirror sync for `?category=` — architecture.md §5 extends the
  // drill-down contract to the 4 retry categories (Retry 审计字段).
  useEffect(() => {
    const current = searchParams.get('category');
    if (categoryFilter === 'all') {
      if (current !== null) {
        const next = new URLSearchParams(searchParams);
        next.delete('category');
        setSearchParams(next, { replace: true });
      }
    } else if (current !== categoryFilter) {
      const next = new URLSearchParams(searchParams);
      next.set('category', categoryFilter);
      setSearchParams(next, { replace: true });
    }
  }, [categoryFilter, searchParams, setSearchParams]);

  // React to external URL changes (e.g. reviewer clicking a second
  // Dashboard link while already on /review) — unlike component mount
  // we can't rely on the initial read; treat URL as source of truth.
  useEffect(() => {
    const raw = searchParams.get('status');
    const want: StatusFilter = isStatusFilter(raw) ? raw : 'all';
    if (want !== statusFilter) {
      setStatusFilter(want);
    }
    const rawCaller = searchParams.get('caller');
    const wantCaller = rawCaller && rawCaller.length > 0 ? rawCaller : null;
    if (wantCaller !== callerFilter) {
      setCallerFilter(wantCaller);
    }
    const rawCategory = searchParams.get('category');
    const wantCategory: CategoryFilter = isCategoryFilter(rawCategory) ? rawCategory : 'all';
    if (wantCategory !== categoryFilter) {
      setCategoryFilter(wantCategory);
    }
    // Intentionally only re-run on searchParams — outgoing sync is
    // covered by the effects above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [u, r] = await Promise.all([
        api.listUnresolved({ limit: 500 }),
        api.getReviews(),
      ]);
      setGaps(u.items);
      setReviews(r);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const filteredGaps = useMemo(() => {
    const term = filter.trim().toLowerCase();
    const byStatus = (g: UnresolvedCall) => {
      if (statusFilter === 'all') return true;
      // GAPs default to "pending" on the backend (schema.py:79) so a
      // missing status is treated as pending for filtering purposes.
      const s = g.status ?? 'pending';
      return s === statusFilter;
    };
    // architecture.md §5 drill-down 契约：?caller=<function_id> 精确匹配
    // caller_id，让 FunctionBrowser 的 GAP chip 一键预筛选该函数的
    // backlog（跨页面检索免手敲 caller_id）。
    const byCaller = (g: UnresolvedCall) => {
      if (!callerFilter) return true;
      return g.caller_id === callerFilter;
    };
    // architecture.md §3 Retry 审计字段 + §5 drill-down 契约：按
    // last_attempt_reason 的 <category>: 前缀精确匹配。未 stamp 过的
    // GAP（category === null）在 all 以外的任何 bucket 都不命中——
    // reviewer 在问"哪些 agent_error 还没修掉"时，把"还没试过"的
    // GAP 算进去只会污染信号。
    const byCategory = (g: UnresolvedCall) => {
      if (categoryFilter === 'all') return true;
      const cat = extractCategory(g.last_attempt_reason);
      return cat === categoryFilter;
    };
    return gaps.filter((g) => {
      if (!byStatus(g)) return false;
      if (!byCaller(g)) return false;
      if (!byCategory(g)) return false;
      if (!term) return true;
      return (
        g.caller_id.toLowerCase().includes(term) ||
        g.call_expression.toLowerCase().includes(term) ||
        g.call_type.toLowerCase().includes(term) ||
        (g.var_name ?? '').toLowerCase().includes(term)
      );
    });
  }, [gaps, filter, statusFilter, callerFilter, categoryFilter]);

  // Counts drive the status-filter chip labels so reviewers see the
  // unresolvable backlog without switching tabs — North Star #5 (state
  // transparency).
  const statusCounts = useMemo(() => {
    let pending = 0;
    let unresolvable = 0;
    for (const g of gaps) {
      const s = g.status ?? 'pending';
      if (s === 'unresolvable') unresolvable += 1;
      else pending += 1;
    }
    return { all: gaps.length, pending, unresolvable };
  }, [gaps]);

  // architecture.md §3 Retry 审计字段 categories. Total-bucket counts
  // let reviewers see backlog skew across the 4 failure modes without
  // clicking each chip (North Star #5 state transparency).
  const categoryCounts = useMemo(() => {
    const counts = {
      all: gaps.length,
      gate_failed: 0,
      agent_error: 0,
      subprocess_crash: 0,
      subprocess_timeout: 0,
    };
    for (const g of gaps) {
      const cat = extractCategory(g.last_attempt_reason);
      if (cat === 'gate_failed') counts.gate_failed += 1;
      else if (cat === 'agent_error') counts.agent_error += 1;
      else if (cat === 'subprocess_crash') counts.subprocess_crash += 1;
      else if (cat === 'subprocess_timeout') counts.subprocess_timeout += 1;
    }
    return counts;
  }, [gaps]);

  // Clamp selectedIndex when the filtered list shrinks/grows.
  useEffect(() => {
    if (selectedIndex === null) return;
    if (filteredGaps.length === 0) {
      setSelectedIndex(null);
    } else if (selectedIndex >= filteredGaps.length) {
      setSelectedIndex(filteredGaps.length - 1);
    }
  }, [filteredGaps, selectedIndex]);

  // Scroll the selected row into view inside the scrollable table container.
  useEffect(() => {
    if (selectedIndex === null) return;
    const container = tableRef.current;
    if (!container) return;
    const row = container.querySelector<HTMLTableRowElement>(
      `tr[data-row-index="${selectedIndex}"]`
    );
    row?.scrollIntoView({ block: 'nearest' });
  }, [selectedIndex]);

  const markCorrect = async (g: UnresolvedCall) => {
    const key = g.id ?? `${g.caller_id}:${g.call_line}`;
    setBusyId(key);
    try {
      // Note: "marking correct" on a GAP means the reviewer acknowledges
      // the agent was right to leave it unresolved. We record this as a
      // review with verdict=correct and callee_id="" (no resolved edge).
      await api.createReview({
        caller_id: g.caller_id,
        callee_id: '',
        call_file: g.call_file,
        call_line: g.call_line,
        verdict: 'correct',
        comment: `marked correct: ${g.call_expression}`,
      });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const markWrong = async (g: UnresolvedCall) => {
    // Open the counter-example modal — the actual POST happens in
    // submitWrong(). Architecture.md §5 requires the reviewer to supply
    // a correct target before we generate a counter example.
    setWrongFor(g);
  };

  const submitWrong = async (
    g: UnresolvedCall,
    correctTarget: string,
    pattern: string
  ) => {
    const key = g.id ?? `${g.caller_id}:${g.call_line}`;
    setBusyId(key);
    try {
      // Persist the counter example so the next repair round picks it up
      // via RepairOrchestrator (architecture.md §3 反馈机制 step 4). The
      // response tells us whether the pattern was newly added or merged
      // into an existing one — surface it as an inline banner so the
      // reviewer sees the dedup outcome without opening FeedbackLog.
      const result = await api.createFeedback({
        call_context: g.call_expression,
        wrong_target: g.candidates?.[0] ?? '(unknown)',
        correct_target: correctTarget,
        pattern: pattern || correctTarget,
      });
      setLastFeedback({
        deduplicated: result.deduplicated,
        total: result.total,
        pattern: result.pattern,
      });
      // Also record the reviewer's decision as a Review — preserves the
      // existing audit trail surfaced in the Reviews tab.
      await api.createReview({
        caller_id: g.caller_id,
        callee_id: correctTarget,
        call_file: g.call_file,
        call_line: g.call_line,
        verdict: 'incorrect',
        comment: `marked wrong: ${g.call_expression} → ${correctTarget}`,
        correct_target: correctTarget,
      });
      setWrongFor(null);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const deleteReview = async (id: string) => {
    setBusyId(id);
    try {
      await api.deleteReview(id);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  // Keyboard shortcuts for the Unresolved GAPs tab.
  // j/↓ next · k/↑ prev · y = mark correct · n = mark wrong · Esc = clear selection.
  useEffect(() => {
    if (tab !== 'gaps') return;

    const handler = (e: KeyboardEvent) => {
      // Never hijack keys while the user is typing into an input/textarea.
      const target = e.target as HTMLElement | null;
      if (target) {
        const tag = target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || target.isContentEditable) {
          return;
        }
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (filteredGaps.length === 0) return;

      const move = (delta: number) => {
        setSelectedIndex((cur) => {
          if (cur === null) return delta > 0 ? 0 : filteredGaps.length - 1;
          const next = cur + delta;
          if (next < 0) return 0;
          if (next >= filteredGaps.length) return filteredGaps.length - 1;
          return next;
        });
      };

      switch (e.key) {
        case 'j':
        case 'ArrowDown':
          e.preventDefault();
          move(1);
          break;
        case 'k':
        case 'ArrowUp':
          e.preventDefault();
          move(-1);
          break;
        case 'y': {
          if (selectedIndex === null) return;
          const g = filteredGaps[selectedIndex];
          if (!g) return;
          e.preventDefault();
          void markCorrect(g);
          break;
        }
        case 'n': {
          if (selectedIndex === null) return;
          const g = filteredGaps[selectedIndex];
          if (!g) return;
          e.preventDefault();
          void markWrong(g);
          break;
        }
        case 'Escape':
          if (selectedIndex !== null) {
            e.preventDefault();
            setSelectedIndex(null);
          }
          break;
      }
    };

    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [tab, filteredGaps, selectedIndex, markCorrect, markWrong]);

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Review Queue</h1>
        <button
          className="px-3 py-1 rounded border text-sm hover:bg-gray-50"
          onClick={refresh}
          disabled={loading}
        >
          Refresh
        </button>
      </div>

      <div className="flex gap-2 border-b">
        <button
          className={`px-3 py-1 text-sm ${
            tab === 'gaps'
              ? 'border-b-2 border-blue-600 text-blue-600'
              : 'text-gray-600'
          }`}
          onClick={() => setTab('gaps')}
        >
          Unresolved GAPs ({gaps.length})
        </button>
        <button
          className={`px-3 py-1 text-sm ${
            tab === 'reviews'
              ? 'border-b-2 border-blue-600 text-blue-600'
              : 'text-gray-600'
          }`}
          onClick={() => setTab('reviews')}
        >
          Reviews ({reviews.length})
        </button>
      </div>

      {error ? (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded p-3 text-sm">
          {error}
        </div>
      ) : null}

      {lastFeedback ? (
        <div
          className={`flex items-start justify-between gap-3 rounded border p-3 text-sm ${
            lastFeedback.deduplicated
              ? 'bg-amber-50 border-amber-200 text-amber-800'
              : 'bg-emerald-50 border-emerald-200 text-emerald-800'
          }`}
        >
          <div className="space-y-0.5">
            <div className="font-medium">
              {lastFeedback.deduplicated
                ? 'Merged into existing counter-example pattern'
                : 'New counter-example pattern saved'}
            </div>
            <div className="text-xs opacity-80">
              {/*
                architecture.md §5 跨页面 drill-down 契约：把 pattern
                链到 FeedbackLog，审阅者一键确认"下一轮 repair CLAUDE.md
                注入的就是这条"（北极星 #5 反例命中）。
              */}
              <Link
                to={`/feedback?pattern=${encodeURIComponent(lastFeedback.pattern)}`}
                className="font-mono break-all underline decoration-dotted underline-offset-2 hover:decoration-solid"
                title="View in Feedback Log"
              >
                {lastFeedback.pattern}
              </Link>
              <span className="mx-2 opacity-60">·</span>
              <span>Library size: {lastFeedback.total}</span>
            </div>
          </div>
          <button
            className="text-xs underline opacity-70 hover:opacity-100 shrink-0"
            onClick={() => setLastFeedback(null)}
          >
            Dismiss
          </button>
        </div>
      ) : null}

      {tab === 'gaps' ? (
        <>
          {callerFilter ? (
            // architecture.md §5 drill-down 契约：回应 `?caller=` — 给
            // 审阅者一个可见、可清除的筛选 chip 表明当前列表已被按函数
            // 预筛选（否则空列表很容易被误读成 backlog 已清或 search
            // 挡住了）。点 × 清除即回写 URL 去掉 ?caller=.
            <div className="flex items-center gap-2 text-xs bg-blue-50 border border-blue-200 rounded px-2 py-1">
              <span className="text-gray-600">Filtering by caller:</span>
              <span
                className="font-mono text-blue-700 truncate max-w-[32rem]"
                title={callerFilter}
              >
                {shorten(callerFilter)}
              </span>
              <button
                className="ml-auto text-[11px] text-blue-700 hover:underline"
                onClick={() => setCallerFilter(null)}
                title="Clear caller filter"
              >
                × Clear
              </button>
            </div>
          ) : null}
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="text-gray-500">Status:</span>
            {(
              [
                { key: 'all', label: 'All', count: statusCounts.all, tone: 'bg-gray-100 text-gray-700 border-gray-300' },
                { key: 'pending', label: 'Pending', count: statusCounts.pending, tone: 'bg-gray-100 text-gray-700 border-gray-300' },
                { key: 'unresolvable', label: 'Unresolvable', count: statusCounts.unresolvable, tone: 'bg-red-50 text-red-700 border-red-300' },
              ] as const
            ).map((opt) => {
              const active = statusFilter === opt.key;
              return (
                <button
                  key={opt.key}
                  className={`px-2 py-0.5 rounded border ${
                    active
                      ? 'bg-blue-600 text-white border-blue-600'
                      : opt.tone + ' hover:bg-gray-50'
                  }`}
                  onClick={() => setStatusFilter(opt.key)}
                  title={`Show ${opt.label.toLowerCase()} GAPs`}
                >
                  {opt.label} ({opt.count})
                </button>
              );
            })}
          </div>
          {/*
            architecture.md §5 drill-down 契约扩展：`?category=<cat>`
            isolates GAPs whose last_attempt_reason starts with one of
            the 4 Retry 审计字段 categories. Tones match GapDetail
            last-attempt 分色 (§5) so chip ↔ panel share one visual
            language — North Star #1 (category triage ≥1 scroll → 1
            click) + #5 (state transparency across list + detail).
          */}
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="text-gray-500">Category:</span>
            {(
              [
                { key: 'all', label: 'All', count: categoryCounts.all, tone: 'bg-gray-100 text-gray-700 border-gray-300' },
                { key: 'gate_failed', label: 'gate_failed', count: categoryCounts.gate_failed, tone: 'bg-amber-50 text-amber-800 border-amber-300' },
                { key: 'agent_error', label: 'agent_error', count: categoryCounts.agent_error, tone: 'bg-red-50 text-red-800 border-red-300' },
                { key: 'subprocess_crash', label: 'subprocess_crash', count: categoryCounts.subprocess_crash, tone: 'bg-fuchsia-50 text-fuchsia-800 border-fuchsia-300' },
                { key: 'subprocess_timeout', label: 'subprocess_timeout', count: categoryCounts.subprocess_timeout, tone: 'bg-orange-50 text-orange-800 border-orange-300' },
              ] as const
            ).map((opt) => {
              const active = categoryFilter === opt.key;
              return (
                <button
                  key={opt.key}
                  className={`px-2 py-0.5 rounded border ${
                    active
                      ? 'bg-blue-600 text-white border-blue-600'
                      : opt.tone + ' hover:brightness-95'
                  }`}
                  onClick={() => setCategoryFilter(opt.key)}
                  title={
                    opt.key === 'all'
                      ? 'Show GAPs regardless of retry category'
                      : `Show GAPs whose last_attempt_reason starts with ${opt.key}:`
                  }
                >
                  <span className="font-mono">{opt.label}</span> ({opt.count})
                </button>
              );
            })}
          </div>
          <input
            className="w-full border rounded px-3 py-1 text-sm"
            placeholder="Filter by caller, expression, call_type, var_name…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <div className="text-xs text-gray-500 flex flex-wrap gap-x-3 gap-y-1">
            <span>
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">j</kbd>/
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">↓</kbd> next
            </span>
            <span>
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">k</kbd>/
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">↑</kbd> prev
            </span>
            <span>
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">y</kbd> mark correct
            </span>
            <span>
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">n</kbd> mark wrong
            </span>
            <span>
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">Esc</kbd> clear
            </span>
            <span className="text-gray-400">
              (disabled while typing in the filter box · selected row expands with call context)
            </span>
          </div>
          <div
            ref={tableRef}
            className="bg-white border rounded shadow-sm overflow-auto max-h-[70vh]"
          >
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50 sticky top-0">
                <tr>
                  <th className="px-3 py-2 text-left font-medium text-gray-500">Type</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-500">Expression</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-500">Caller</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-500">Location</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-500">Candidates</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-500">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {loading ? (
                  <tr>
                    <td colSpan={6} className="px-3 py-6 text-center text-gray-500">
                      Loading…
                    </td>
                  </tr>
                ) : filteredGaps.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="px-3 py-8 text-center">
                      {gaps.length === 0 ? (
                        // Truly empty — distinguish from filter-hidden so
                        // reviewers landing here via Dashboard/nav drill-down
                        // (architecture.md §5) don't misread a clean graph
                        // as a broken filter. North Star #5 state
                        // transparency.
                        <div className="space-y-1">
                          <div className="text-sm font-medium text-green-700">
                            All GAPs resolved.
                          </div>
                          <div className="text-xs text-gray-500">
                            Trigger the repair agent from the Dashboard when
                            new unresolved calls appear.
                          </div>
                        </div>
                      ) : (
                        // Filter-hidden — surface how many rows the current
                        // filters are swallowing and give a one-click escape
                        // so a stale deep-link from the nav/StatCard chip
                        // (e.g. `?status=unresolvable` after the backlog
                        // cleared between poll + click) doesn't look broken.
                        // North Star #1 (review time — no guessing why the
                        // list is empty) + candidate #4 (observability).
                        <div className="space-y-2">
                          <div className="text-sm font-medium text-gray-700">
                            No GAPs match the current filters.
                          </div>
                          <div className="text-xs text-gray-500">
                            {gaps.length} GAP{gaps.length === 1 ? '' : 's'}{' '}
                            hidden by{' '}
                            {(() => {
                              const parts: ReactNode[] = [];
                              if (statusFilter !== 'all') {
                                parts.push(
                                  <span key="s">
                                    status=
                                    <span className="font-mono">{statusFilter}</span>
                                  </span>
                                );
                              }
                              if (callerFilter) {
                                parts.push(
                                  <span key="c">
                                    caller=
                                    <span className="font-mono">
                                      {shorten(callerFilter)}
                                    </span>
                                  </span>
                                );
                              }
                              if (categoryFilter !== 'all') {
                                parts.push(
                                  <span key="cat">
                                    category=
                                    <span className="font-mono">{categoryFilter}</span>
                                  </span>
                                );
                              }
                              if (filter.trim()) {
                                parts.push(
                                  <span key="q">
                                    search=
                                    <span className="font-mono">
                                      &ldquo;{filter.trim()}&rdquo;
                                    </span>
                                  </span>
                                );
                              }
                              return parts.map((p, i) => (
                                <span key={i}>
                                  {i > 0 ? ' and ' : ''}
                                  {p}
                                </span>
                              ));
                            })()}
                            .
                          </div>
                          <div className="flex justify-center gap-2 pt-1">
                            {statusFilter !== 'all' ? (
                              <button
                                className="px-2 py-0.5 rounded border text-xs hover:bg-gray-50"
                                onClick={() => setStatusFilter('all')}
                              >
                                Show all statuses
                              </button>
                            ) : null}
                            {callerFilter ? (
                              <button
                                className="px-2 py-0.5 rounded border text-xs hover:bg-gray-50"
                                onClick={() => setCallerFilter(null)}
                              >
                                Clear caller filter
                              </button>
                            ) : null}
                            {categoryFilter !== 'all' ? (
                              <button
                                className="px-2 py-0.5 rounded border text-xs hover:bg-gray-50"
                                onClick={() => setCategoryFilter('all')}
                              >
                                Show all categories
                              </button>
                            ) : null}
                            {filter.trim() ? (
                              <button
                                className="px-2 py-0.5 rounded border text-xs hover:bg-gray-50"
                                onClick={() => setFilter('')}
                              >
                                Clear search
                              </button>
                            ) : null}
                          </div>
                        </div>
                      )}
                    </td>
                  </tr>
                ) : (
                  filteredGaps.map((g, i) => {
                    const key = g.id ?? `${g.caller_id}:${g.call_line}:${i}`;
                    const busy = busyId === key;
                    const selected = i === selectedIndex;
                    return (
                      <Fragment key={key}>
                      <tr
                        data-row-index={i}
                        onClick={() => setSelectedIndex(i)}
                        className={`align-top cursor-pointer ${
                          selected
                            ? 'bg-blue-50 ring-2 ring-inset ring-blue-400'
                            : 'hover:bg-gray-50'
                        }`}
                      >
                        <td className="px-3 py-2">
                          <div className="flex flex-col gap-1">
                            <span className="inline-block px-2 py-0.5 rounded bg-amber-50 text-amber-700 text-xs self-start">
                              {g.call_type}
                            </span>
                            <GapStatusChips gap={g} />
                          </div>
                        </td>
                        <td className="px-3 py-2 font-mono text-xs">
                          {g.call_expression}
                        </td>
                        <td
                          className="px-3 py-2 font-mono text-xs text-gray-600"
                          title={g.caller_id}
                        >
                          {/*
                            architecture.md §5 跨页面 drill-down 契约：
                            caller → CallGraphView 预选中。CallGraphView
                            已经消费 `?function=` 并高亮 root，审阅者看
                            GAP 的同时一次点击就能看到它在调用链里是哪
                            条 edge（北极星 #1 GAP 审阅耗时 + #2 调用链
                            可信度可见性）。stopPropagation 避免点击时
                            顺手触发行选中——否则导航前行会闪一下背景。
                          */}
                          <Link
                            to={`/graph?function=${encodeURIComponent(g.caller_id)}`}
                            onClick={(e) => e.stopPropagation()}
                            className="text-blue-600 hover:underline decoration-dotted underline-offset-2 hover:decoration-solid"
                            title={`Open call graph for ${g.caller_id}`}
                          >
                            {shorten(g.caller_id)}
                          </Link>
                        </td>
                        <td className="px-3 py-2 font-mono text-xs text-gray-600">
                          {shorten(g.call_file)}:{g.call_line}
                        </td>
                        <td className="px-3 py-2 text-xs text-gray-600">
                          {g.candidates && g.candidates.length > 0 ? (
                            <details>
                              <summary>{g.candidates.length} candidates</summary>
                              <ul className="mt-1 pl-4 list-disc">
                                {g.candidates.slice(0, 10).map((c) => (
                                  <li key={c} className="font-mono break-all">
                                    {c}
                                  </li>
                                ))}
                                {g.candidates.length > 10 ? (
                                  <li>…and {g.candidates.length - 10} more</li>
                                ) : null}
                              </ul>
                            </details>
                          ) : (
                            <span className="text-gray-400">—</span>
                          )}
                        </td>
                        <td className="px-3 py-2">
                          <div className="flex gap-1">
                            <button
                              className="px-2 py-0.5 rounded bg-green-100 text-green-700 text-xs hover:bg-green-200 disabled:opacity-50"
                              onClick={() => markCorrect(g)}
                              disabled={busy}
                            >
                              Mark correct
                            </button>
                            <button
                              className="px-2 py-0.5 rounded bg-red-100 text-red-700 text-xs hover:bg-red-200 disabled:opacity-50"
                              onClick={() => markWrong(g)}
                              disabled={busy}
                              title="Trigger counter-example generation"
                            >
                              Mark wrong
                            </button>
                          </div>
                        </td>
                      </tr>
                      {selected ? (
                        <tr className="bg-blue-50/60">
                          <td colSpan={6} className="px-3 pb-3 pt-0">
                            <GapDetail gap={g} />
                          </td>
                        </tr>
                      ) : null}
                      </Fragment>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <div className="bg-white border rounded shadow-sm overflow-auto">
          <table className="min-w-full divide-y divide-gray-200 text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-left font-medium text-gray-500">Verdict</th>
                <th className="px-3 py-2 text-left font-medium text-gray-500">Edge</th>
                <th className="px-3 py-2 text-left font-medium text-gray-500">Comment</th>
                <th className="px-3 py-2 text-left font-medium text-gray-500">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {reviews.length === 0 ? (
                <tr>
                  <td colSpan={4} className="px-3 py-6 text-center text-gray-500">
                    No reviews yet.
                  </td>
                </tr>
              ) : (
                reviews.map((r) => (
                  <tr key={r.id} className="hover:bg-gray-50 align-top">
                    <td className="px-3 py-2">
                      <span
                        className={`inline-block px-2 py-0.5 rounded text-xs ${
                          r.verdict === 'correct'
                            ? 'bg-green-100 text-green-700'
                            : 'bg-red-100 text-red-700'
                        }`}
                      >
                        {r.verdict}
                      </span>
                    </td>
                    <td
                      className="px-3 py-2 font-mono text-xs text-gray-600"
                      title={`${r.caller_id} → ${r.callee_id}`}
                    >
                      {shorten(r.caller_id)} → {shorten(r.callee_id)}
                    </td>
                    <td className="px-3 py-2 text-xs text-gray-700">
                      {r.comment}
                    </td>
                    <td className="px-3 py-2">
                      <button
                        className="text-xs text-red-600 hover:underline disabled:opacity-50"
                        onClick={() => deleteReview(r.id)}
                        disabled={busyId === r.id}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
      {wrongFor ? (
        <MarkWrongModal
          gap={wrongFor}
          busy={
            busyId === (wrongFor.id ?? `${wrongFor.caller_id}:${wrongFor.call_line}`)
          }
          onCancel={() => setWrongFor(null)}
          onSubmit={(correct, pattern) => submitWrong(wrongFor, correct, pattern)}
        />
      ) : null}
    </div>
  );
}

function MarkWrongModal({
  gap,
  busy,
  onCancel,
  onSubmit,
}: {
  gap: UnresolvedCall;
  busy: boolean;
  onCancel: () => void;
  onSubmit: (correctTarget: string, pattern: string) => void;
}) {
  const [correctTarget, setCorrectTarget] = useState('');
  const [pattern, setPattern] = useState('');

  const canSubmit = correctTarget.trim().length > 0 && !busy;

  return (
    <div
      className="fixed inset-0 bg-black/40 flex items-center justify-center p-4 z-50"
      onClick={onCancel}
    >
      <div
        className="bg-white rounded shadow-lg w-full max-w-md p-4 space-y-3"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold">Mark as wrong — add counter example</h2>
        <p className="text-xs text-gray-500">
          Architecture &sect;5: fill the correct target so the next repair
          round avoids this mistake. The pattern (generalized rule) is
          injected into the agent&rsquo;s <code>CLAUDE.md</code>.
        </p>

        <dl className="text-xs bg-gray-50 border rounded p-2 space-y-1">
          <div className="flex gap-2">
            <dt className="text-gray-500 w-24 shrink-0">call</dt>
            <dd className="font-mono break-all">{gap.call_expression}</dd>
          </div>
          <div className="flex gap-2">
            <dt className="text-gray-500 w-24 shrink-0">wrong target</dt>
            <dd className="font-mono break-all text-red-700">
              {gap.candidates?.[0] ?? '(unknown)'}
            </dd>
          </div>
        </dl>

        <label className="block text-sm">
          <span className="text-gray-700">Correct target</span>
          <input
            autoFocus
            className="mt-1 w-full border rounded px-2 py-1 text-sm font-mono"
            placeholder="e.g. modern_handler"
            value={correctTarget}
            onChange={(e) => setCorrectTarget(e.target.value)}
          />
        </label>

        <label className="block text-sm">
          <span className="text-gray-700">
            Generalized pattern{' '}
            <span className="text-gray-400">(optional — defaults to correct target)</span>
          </span>
          <textarea
            className="mt-1 w-full border rounded px-2 py-1 text-sm"
            rows={2}
            placeholder="e.g. dispatcher vtable resolution must prefer modern_handler"
            value={pattern}
            onChange={(e) => setPattern(e.target.value)}
          />
        </label>

        <div className="flex justify-end gap-2 pt-1">
          <button
            className="px-3 py-1 rounded border text-sm hover:bg-gray-50"
            onClick={onCancel}
            disabled={busy}
          >
            Cancel
          </button>
          <button
            className="px-3 py-1 rounded bg-red-600 text-white text-sm hover:bg-red-700 disabled:opacity-50"
            onClick={() => onSubmit(correctTarget.trim(), pattern.trim())}
            disabled={!canSubmit}
          >
            {busy ? 'Saving…' : 'Save counter example'}
          </button>
        </div>
      </div>
    </div>
  );
}

function GapDetail({ gap }: { gap: UnresolvedCall }) {
  const snippet = gap.source_code_snippet?.trim();
  const chips: { label: string; value: string; tone: string }[] = [];
  if (gap.var_name) {
    chips.push({ label: 'var', value: gap.var_name, tone: 'bg-slate-100 text-slate-700' });
  }
  if (gap.var_type) {
    chips.push({ label: 'type', value: gap.var_type, tone: 'bg-slate-100 text-slate-700' });
  }

  // architecture.md §3 Retry 审计字段: show the orchestrator-stamped
  // last-attempt reason + timestamp inline so reviewers don't have to
  // open logs/repair/<source>/*.jsonl to see why the agent gave up
  // (North Star #1 review time + #5 state transparency).
  const lastReason = gap.last_attempt_reason?.trim();
  const lastTimestamp = gap.last_attempt_timestamp?.trim();
  const hasLastAttempt = Boolean(lastReason || lastTimestamp);
  // architecture.md §5 GapDetail last-attempt 分色: 4 categories → 4 distinct
  // tones so reviewers can read the failure class at a glance
  // (gate_failed=amber soft retry-more, agent_error=red agent logic failure,
  // subprocess_crash=fuchsia spawn failure/ops config, subprocess_timeout=
  // orange ops stall). Unknown/legacy → gray fallback.
  const category = lastReason?.split(':', 1)[0].trim();
  const categoryTone =
    category === 'gate_failed'
      ? 'bg-amber-50 border-amber-200 text-amber-800'
      : category === 'agent_error'
      ? 'bg-red-50 border-red-200 text-red-800'
      : category === 'subprocess_crash'
      ? 'bg-fuchsia-50 border-fuchsia-200 text-fuchsia-800'
      : category === 'subprocess_timeout'
      ? 'bg-orange-50 border-orange-200 text-orange-800'
      : 'bg-gray-50 border-gray-200 text-gray-800';
  const humanTimestamp = lastTimestamp
    ? (() => {
        const d = new Date(lastTimestamp);
        return Number.isNaN(d.getTime()) ? lastTimestamp : d.toLocaleString();
      })()
    : null;

  return (
    <div className="rounded border border-blue-200 bg-white p-3 space-y-2">
      {chips.length > 0 || gap.status || typeof gap.retry_count === 'number' ? (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {chips.map((c) => (
            <span
              key={`${c.label}:${c.value}`}
              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded ${c.tone}`}
            >
              <span className="text-[10px] uppercase tracking-wide opacity-70">{c.label}</span>
              <span className="font-mono break-all">{c.value}</span>
            </span>
          ))}
          <GapStatusChips gap={gap} size="md" />
        </div>
      ) : null}
      {hasLastAttempt ? (
        <div className={`rounded border px-2 py-1.5 text-xs ${categoryTone}`}>
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-wide opacity-70">
              last attempt
            </span>
            {humanTimestamp ? (
              <span
                className="font-mono text-[11px] opacity-80"
                title={lastTimestamp ?? undefined}
              >
                {humanTimestamp}
              </span>
            ) : null}
          </div>
          {lastReason ? (
            <div className="font-mono break-words mt-0.5">{lastReason}</div>
          ) : null}
        </div>
      ) : null}
      {snippet ? (
        <pre className="text-xs font-mono bg-gray-50 border rounded p-2 overflow-x-auto whitespace-pre">
          {snippet}
        </pre>
      ) : (
        <div className="text-xs text-gray-400 italic">No source snippet captured for this call.</div>
      )}
      {gap.candidates && gap.candidates.length > 0 ? (
        <div className="text-xs text-gray-600">
          <div className="font-medium text-gray-700 mb-1">
            Candidates ({gap.candidates.length})
          </div>
          <ul className="pl-4 list-disc space-y-0.5">
            {gap.candidates.slice(0, 20).map((c) => (
              <li key={c} className="font-mono break-all">
                {c}
              </li>
            ))}
            {gap.candidates.length > 20 ? (
              <li className="text-gray-500">…and {gap.candidates.length - 20} more</li>
            ) : null}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
