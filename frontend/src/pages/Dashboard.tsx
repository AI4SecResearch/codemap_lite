import { useEffect, useState, useCallback, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { Target, FileCode, Code2, GitFork, Bot, AlertTriangle, TrendingUp, Activity, Zap, RefreshCw, Play, Wrench } from 'lucide-react';
import {
  api,
  Stats,
  AnalyzeStatus,
  SourceProgress,
  FunctionNode,
} from '../api/client';
import { Card, ProgressBar, Button, EmptyState } from '../components/ui';

// architecture.md §5 跨页面 drill-down 契约：Dashboard "Top backlog
// functions" 取 api.listUnresolved 客户端按 caller_id 聚合后降序前 5，
// 每行 <Link to="/sources?caller=<id>">——打开 Dashboard 即见热点函数，
// 1 次点击落到预筛选 GAP 列表，免绕 FunctionBrowser（北极星 #1 + #5）。
const TOP_BACKLOG_LIMIT = 5;

type BacklogRow = { callerId: string; name: string; count: number };

// architecture.md §5 跨页面 drill-down 契约：Dashboard StatCard
// 可选 `to` 让卡片本身承载跳转（pre-filtered 子视图），减少审阅者
// 从"看到 backlog 数字"到"打开对应筛选列表"的点击数（北极星指标 #1）。
function StatCard({
  title,
  value,
  hint,
  tone = 'default',
  to,
  icon,
}: {
  title: string;
  value: number | string;
  hint?: string;
  tone?: 'default' | 'alert' | 'warn';
  to?: string;
  icon?: React.ReactNode;
}) {
  const toneClasses =
    tone === 'alert'
      ? 'bg-gradient-to-br from-red-50 to-red-100/50 border-red-200'
      : tone === 'warn'
      ? 'bg-gradient-to-br from-amber-50 to-amber-100/50 border-amber-200'
      : 'bg-white border-gray-200';
  const valueClasses =
    tone === 'alert'
      ? 'text-red-700'
      : tone === 'warn'
      ? 'text-amber-700'
      : 'text-gray-900';
  const interactive = to
    ? 'cursor-pointer hover:shadow-card-hover hover:-translate-y-0.5 transition-all duration-200 focus:outline-none focus:ring-2 focus:ring-blue-400'
    : '';
  const body = (
    <>
      <div className="text-xs uppercase tracking-wide text-gray-500 flex items-center gap-1.5">
        {icon}
        <span>{title}</span>
        {to ? <span aria-hidden className="text-gray-400">›</span> : null}
      </div>
      <div className={`text-3xl font-bold mt-1 tabular-nums ${valueClasses}`}>{value}</div>
      {hint ? <div className="text-xs text-gray-500 mt-1">{hint}</div> : null}
    </>
  );
  if (to) {
    return (
      <Link to={to} className={`${toneClasses} ${interactive} rounded-xl shadow-card border p-4 block no-underline text-inherit`}>
        {body}
      </Link>
    );
  }
  return <div className={`${toneClasses} rounded-xl shadow-card border p-4`}>{body}</div>;
}

function SourceProgressCard({ row }: { row: SourceProgress }) {
  const [funcName, setFuncName] = useState<string | null>(null);
  const pct =
    row.gaps_total > 0
      ? Math.min(100, Math.round((row.gaps_fixed / row.gaps_total) * 100))
      : 0;
  const done = row.gaps_total > 0 && row.gaps_fixed >= row.gaps_total;
  const barColor = done ? 'bg-green-500' : 'bg-blue-500';

  useEffect(() => {
    if (row.source_id) {
      api.getFunction(row.source_id)
        .then((fn) => setFuncName(fn.name || fn.signature?.split('(')[0] || null))
        .catch(() => {});
    }
  }, [row.source_id]);

  // State indicator dot
  const stateColor =
    row.state === 'running'
      ? 'bg-blue-500 animate-pulse'
      : row.state === 'gate_checking'
      ? 'bg-amber-500 animate-pulse'
      : row.state === 'succeeded'
      ? 'bg-green-500'
      : row.state === 'failed'
      ? 'bg-red-500'
      : 'bg-gray-300';

  // Gate result chip
  const gateChip =
    row.gate_result === 'passed'
      ? { text: 'gate passed', cls: 'bg-green-100 text-green-800' }
      : row.gate_result === 'failed'
      ? { text: 'gate failed', cls: 'bg-amber-100 text-amber-800' }
      : null;

  return (
    <div className="bg-white rounded border shadow-sm p-3 space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className={`w-2 h-2 rounded-full shrink-0 ${stateColor}`} />
          <div
            className="text-xs text-gray-700"
            title={row.source_id}
          >
            {funcName ?? row.source_id}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {row.attempt != null && row.max_attempts != null ? (
            <span className="text-[10px] font-mono bg-gray-100 text-gray-600 rounded px-1 py-0.5">
              {row.attempt}/{row.max_attempts}
            </span>
          ) : null}
          <div className="text-xs text-gray-500">
            {row.gaps_fixed}/{row.gaps_total}
          </div>
        </div>
      </div>
      <div className="h-1.5 w-full rounded bg-gray-100 overflow-hidden">
        <div
          className={`h-full ${barColor}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex items-center justify-between gap-2">
        <div className="text-[11px] text-gray-500 truncate">
          {done ? (
            <span className="text-green-700">done</span>
          ) : row.current_gap ? (
            <>
              current:{' '}
              <span className="font-mono text-gray-700">{row.current_gap}</span>
            </>
          ) : row.state === 'failed' ? (
            <span className="text-red-600">failed</span>
          ) : (
            <span className="text-gray-400">
              {row.state === 'running' ? 'agent running…' : row.state === 'gate_checking' ? 'checking gate…' : 'idle'}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {row.edges_written != null && row.edges_written > 0 ? (
            <span className="text-[10px] text-orange-700 bg-orange-50 rounded px-1 py-0.5">
              {row.edges_written} edge{row.edges_written === 1 ? '' : 's'}
            </span>
          ) : null}
          {gateChip ? (
            <span className={`text-[10px] rounded px-1 py-0.5 ${gateChip.cls}`}>
              {gateChip.text}
            </span>
          ) : null}
        </div>
      </div>
      {row.last_error ? (
        <div className="text-[10px] text-red-600 truncate" title={row.last_error}>
          {row.last_error}
        </div>
      ) : null}
    </div>
  );
}

export default function Dashboard() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [status, setStatus] = useState<AnalyzeStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Top-backlog widget data. Kept separate from `stats` so a failed
  // listUnresolved/getFunctions call doesn't blank out the StatCards.
  const [functions, setFunctions] = useState<FunctionNode[]>([]);
  const [gapCounts, setGapCounts] = useState<Map<string, number>>(new Map());

  const refresh = useCallback(async () => {
    try {
      // api.listUnresolved ceiling of 500 mirrors FunctionBrowser —
      // covers CastEngine with headroom. getFunctions lets us show
      // a readable caller name instead of the raw id. Both are
      // non-blocking: a failure just leaves the widget empty.
      const [s, st, fns, unresolved] = await Promise.all([
        api.getStats(),
        api.getAnalyzeStatus(),
        api.getFunctions().catch(() => ({ total: 0, items: [] as FunctionNode[] })),
        api
          .listUnresolved({ limit: 500 })
          .catch(() => ({ total: 0, items: [] })),
      ]);
      setStats(s);
      setStatus(st);
      setFunctions(fns.items);
      const counts = new Map<string, number>();
      for (const g of unresolved.items) {
        counts.set(g.caller_id, (counts.get(g.caller_id) ?? 0) + 1);
      }
      setGapCounts(counts);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [refresh]);

  const onAnalyze = async (mode: 'full' | 'incremental') => {
    setBusy(true);
    try {
      await api.triggerAnalyze(mode);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onRepair = async () => {
    setBusy(true);
    try {
      await api.triggerRepair();
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const resolvedPct =
    stats && stats.total_calls + stats.total_unresolved > 0
      ? Math.round(
          (stats.total_calls / (stats.total_calls + stats.total_unresolved)) * 100
        )
      : null;

  const byStatus = stats?.unresolved_by_status ?? {};
  const pendingGaps = byStatus.pending ?? 0;
  const unresolvableGaps = byStatus.unresolvable ?? 0;
  const unresolvedHint =
    stats && stats.total_unresolved > 0
      ? `${pendingGaps} pending · ${unresolvableGaps} unresolvable`
      : 'needs repair';

  // LLM-repaired CALLS edges are the review-critical population
  // (architecture.md §5 审阅对象：单条 CALLS 边，特别是 resolved_by='llm').
  // Surface the count so reviewers see the backlog without drilling
  // into SourcePointList (北极星指标 #2 调用链可信度).
  const byResolved = stats?.calls_by_resolved_by ?? {};
  const llmCalls = byResolved.llm ?? 0;
  const resolvedHint =
    resolvedPct !== null
      ? llmCalls > 0
        ? `${resolvedPct}% resolved · ${llmCalls} via llm`
        : `${resolvedPct}% resolved`
      : undefined;

  // architecture.md §5 drill-down 契约: Dashboard "Retry reasons" chip
  // row consumes `/api/v1/stats` unresolved_by_category bucket.
  // Chip tones mirror GapDetail last-attempt 分色 (§3 Retry 审计字段):
  // gate_failed=amber, agent_error=red, subprocess_crash=fuchsia,
  // subprocess_timeout=orange, agent_exited_without_edge=sky, none=gray.
  // Each chip is a drill-down link to `/sources?category=<cat>` so reviewers
  // can go from "25 of 30 unresolvable are subprocess_timeout" to the
  // pre-filtered list in one click (北极星指标 #1 + #5).
  const byCategory = stats?.unresolved_by_category ?? {};
  const CATEGORY_ROW: {
    key: string;
    label: string;
    tone: string;
    title: string;
  }[] = [
    {
      key: 'gate_failed',
      label: 'gate_failed',
      tone: 'bg-amber-100 text-amber-800 hover:bg-amber-200',
      title: 'Agent ran but gate still saw pending GAPs — soft failure',
    },
    {
      key: 'agent_error',
      label: 'agent_error',
      tone: 'bg-red-100 text-red-800 hover:bg-red-200',
      title: 'Agent spawned but exited non-zero (quota / hook / LLM error)',
    },
    {
      key: 'subprocess_crash',
      label: 'subprocess_crash',
      tone: 'bg-fuchsia-100 text-fuchsia-800 hover:bg-fuchsia-200',
      title: 'Spawn itself failed (binary missing / path / permission) — ops',
    },
    {
      key: 'subprocess_timeout',
      label: 'subprocess_timeout',
      tone: 'bg-orange-100 text-orange-800 hover:bg-orange-200',
      title: 'Agent hung past subprocess_timeout_seconds — ops (LLM / net)',
    },
    {
      key: 'agent_exited_without_edge',
      label: 'no_edge',
      tone: 'bg-sky-100 text-sky-800 hover:bg-sky-200',
      title: 'Agent exited cleanly but wrote zero edges — likely gave up or hit dead end',
    },
    {
      key: 'none',
      label: 'none',
      tone: 'bg-gray-100 text-gray-700 hover:bg-gray-200',
      title: 'GAPs without an audit stamp yet (never retried or legacy format)',
    },
  ];
  const categoryRowTotal = CATEGORY_ROW.reduce(
    (sum, row) => sum + (byCategory[row.key] ?? 0),
    0,
  );

  // Top callers by unresolved GAP count (architecture.md §5 跨页面
  // drill-down 契约). Sort desc, slice top N, join with FunctionNode
  // so we can show the readable name instead of the raw id. Unknown
  // ids still render (fallback to trimmed id) so stale gap records
  // don't silently disappear.
  const topBacklog = useMemo<BacklogRow[]>(() => {
    if (gapCounts.size === 0) return [];
    const fnById = new Map<string, FunctionNode>();
    for (const f of functions) fnById.set(f.id, f);
    const rows: BacklogRow[] = [];
    for (const [callerId, count] of gapCounts.entries()) {
      const fn = fnById.get(callerId);
      rows.push({ callerId, name: fn?.name ?? callerId, count });
    }
    rows.sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
    return rows.slice(0, TOP_BACKLOG_LIMIT);
  }, [gapCounts, functions]);
  const totalCallersWithGaps = gapCounts.size;

  // Historical trend: group repair logs by day
  const [trend, setTrend] = useState<{ label: string; count: number }[]>([]);
  useEffect(() => {
    api.getRepairLogs({ limit: 500 }).then((r) => {
      const byDay = new Map<string, number>();
      const now = new Date();
      for (const log of r.items) {
        const d = new Date(log.timestamp);
        const daysAgo = Math.floor((now.getTime() - d.getTime()) / 86400000);
        const label = daysAgo === 0 ? '今天' : daysAgo === 1 ? '昨天' : `${daysAgo}天前`;
        byDay.set(label, (byDay.get(label) ?? 0) + 1);
      }
      setTrend([...byDay.entries()].slice(0, 5).map(([label, count]) => ({ label, count })));
    }).catch(() => {});
  }, []);

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">codemap-lite Dashboard</h1>
        <span
          className={`inline-flex items-center gap-2 px-2 py-1 rounded text-xs ${
            status?.state === 'idle'
              ? 'bg-gray-100 text-gray-700'
              : status?.state === 'running'
              ? 'bg-blue-100 text-blue-700'
              : status?.state === 'repairing'
              ? 'bg-amber-100 text-amber-700'
              : 'bg-gray-100 text-gray-700'
          }`}
        >
          <span className="w-2 h-2 rounded-full bg-current" />
          {status?.state ?? 'unknown'}
          {status && status.progress > 0
            ? ` · ${Math.round(status.progress * 100)}%`
            : ''}
        </span>
      </div>

      {error ? (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded p-3 text-sm">
          {error}
        </div>
      ) : null}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-7 gap-4">
        <StatCard
          title="Source Points"
          value={stats?.total_source_points ?? '-'}
          hint="archdoc entry points"
          to="/sources"
          icon={<Target className="w-3.5 h-3.5 text-blue-500" />}
        />
        <StatCard
          title="Files"
          value={stats?.total_files ?? '-'}
          to="/functions"
          icon={<FileCode className="w-3.5 h-3.5 text-gray-500" />}
        />
        <StatCard
          title="Functions"
          value={stats?.total_functions ?? '-'}
          to="/functions"
          icon={<Code2 className="w-3.5 h-3.5 text-gray-500" />}
        />
        <StatCard
          title="Resolved Calls"
          value={stats?.total_calls ?? '-'}
          hint={resolvedHint}
          icon={<GitFork className="w-3.5 h-3.5 text-green-500" />}
        />
        <StatCard
          title="LLM Repaired"
          value={stats?.total_llm_edges ?? llmCalls ?? '-'}
          hint="needs review (resolved_by=llm)"
          tone={llmCalls > 0 ? 'warn' : 'default'}
          icon={<Bot className="w-3.5 h-3.5 text-amber-500" />}
        />
        <StatCard
          title="Unresolved GAPs"
          value={stats?.total_unresolved ?? '-'}
          hint={unresolvedHint}
          to="/sources?status=pending"
          icon={<AlertTriangle className="w-3.5 h-3.5 text-amber-500" />}
        />
        <StatCard
          title="Unresolvable"
          value={stats ? unresolvableGaps : '-'}
          hint="agent gave up (>=3 retries)"
          tone={unresolvableGaps > 0 ? 'alert' : 'default'}
          to="/sources?status=unresolvable"
          icon={<AlertTriangle className="w-3.5 h-3.5 text-red-500" />}
        />
      </div>

      {categoryRowTotal > 0 ? (
        <Card className="p-3">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs uppercase tracking-wide text-gray-500">
              Retry reasons
            </span>
            {CATEGORY_ROW.map((row) => {
              const count = byCategory[row.key] ?? 0;
              if (count === 0) return null;
              return (
                <Link
                  key={row.key}
                  to={`/sources?category=${encodeURIComponent(row.key)}`}
                  title={row.title}
                  className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium no-underline transition-colors ${row.tone}`}
                >
                  <span className="font-mono">{row.label}</span>
                  <span className="font-semibold">{count}</span>
                </Link>
              );
            })}
            <span className="text-xs text-gray-400 ml-auto">
              click a chip to filter the review queue
            </span>
          </div>
        </Card>
      ) : null}

      <Card className="p-4">
        <h2 className="font-semibold mb-3 flex items-center gap-2"><Activity className="w-4 h-4 text-blue-600" /> Pipeline Actions</h2>
        <div className="flex flex-wrap gap-2">
          <Button icon={<Play className="w-4 h-4" />} onClick={() => onAnalyze('full')} disabled={busy || status?.state === 'running'}>Full Analysis</Button>
          <Button variant="secondary" icon={<Zap className="w-4 h-4" />} onClick={() => onAnalyze('incremental')} disabled={busy || status?.state === 'running'}>Incremental</Button>
          <Button variant="secondary" icon={<Wrench className="w-4 h-4" />} onClick={onRepair} disabled={busy || status?.state === 'repairing'}>Repair Agent</Button>
          <Button variant="ghost" icon={<RefreshCw className="w-4 h-4" />} onClick={refresh} disabled={busy}>Refresh</Button>
        </div>
        <p className="text-xs text-gray-500 mt-3">Status polls every 2s. Triggering analysis is async on the server.</p>
      </Card>

      {/* Repair Effectiveness */}
      {stats && (stats.total_calls > 0 || stats.total_unresolved > 0) && (
        <Card className="p-4">
          <h2 className="font-semibold mb-3 flex items-center gap-2"><TrendingUp className="w-4 h-4 text-green-600" /> Repair Effectiveness</h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <Link to="/sources" className="text-center p-3 rounded-lg bg-green-50 border border-green-100 no-underline hover:shadow-card-hover transition-shadow">
              <div className="text-2xl font-bold text-green-700 tabular-nums">{resolvedPct ?? 0}%</div>
              <div className="text-xs text-green-600 mt-1">Resolution Rate</div>
            </Link>
            <Link to="/graph" className="text-center p-3 rounded-lg bg-blue-50 border border-blue-100 no-underline hover:shadow-card-hover transition-shadow">
              <div className="text-2xl font-bold text-blue-700 tabular-nums">+{stats.total_llm_edges ?? llmCalls}</div>
              <div className="text-xs text-blue-600 mt-1">LLM Edges Added</div>
            </Link>
            <Link to="/feedback" className="text-center p-3 rounded-lg bg-purple-50 border border-purple-100 no-underline hover:shadow-card-hover transition-shadow">
              <div className="text-2xl font-bold text-purple-700 tabular-nums">{stats.total_feedback ?? 0}</div>
              <div className="text-xs text-purple-600 mt-1">Counter-examples</div>
            </Link>
          </div>
          {stats.total_calls + stats.total_unresolved > 0 && (
            <div className="mt-3">
              <ProgressBar value={stats.total_calls / (stats.total_calls + stats.total_unresolved)} size="md" />
            </div>
          )}
          {trend.length > 0 && (
            <div className="text-sm text-gray-600 mt-3">
              {trend.map((t, i) => <span key={i}>{i > 0 && ' · '}{t.label} +{t.count} edges</span>)}
            </div>
          )}
        </Card>
      )}

      <Card className="p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold">Top backlog functions</h2>
          <span className="text-xs text-gray-500">
            {topBacklog.length > 0
              ? `top ${topBacklog.length} of ${totalCallersWithGaps}`
              : '0 callers'}
          </span>
        </div>
        {topBacklog.length > 0 ? (
          <ul className="divide-y">
            {topBacklog.map((row, idx) => {
              const tone =
                row.count >= 3
                  ? 'bg-red-100 text-red-800'
                  : 'bg-amber-100 text-amber-800';
              return (
                <li key={row.callerId}>
                  <Link
                    to={`/sources?caller=${encodeURIComponent(row.callerId)}`}
                    className="flex items-center gap-3 px-2 py-2 rounded hover:bg-gray-50 no-underline text-inherit"
                    title={`${row.count} unresolved GAP${row.count === 1 ? '' : 's'} — review ${row.callerId}`}
                  >
                    <span className="w-5 text-right text-xs text-gray-400 shrink-0">
                      {idx + 1}
                    </span>
                    <span
                      className="font-mono text-xs truncate flex-1"
                      title={row.callerId}
                    >
                      {row.name}
                    </span>
                    <span
                      className={`shrink-0 inline-flex items-center justify-center min-w-[1.5rem] px-1.5 rounded-full text-[11px] font-semibold leading-[1.125rem] ${tone}`}
                    >
                      {row.count}
                    </span>
                    <span aria-hidden className="text-gray-400 text-xs">
                      ›
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        ) : (
          <EmptyState title="No unresolved GAPs" description="Run the repair agent to surface per-function backlog." />
        )}
      </Card>

      <Card className="p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold">Repair Progress</h2>
          <span className="text-xs text-gray-500">
            {status?.sources?.length ?? 0} source
            {(status?.sources?.length ?? 0) === 1 ? '' : 's'}
          </span>
        </div>
        {status?.sources && status.sources.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {status.sources.map((row) => (
              <SourceProgressCard key={row.source_id} row={row} />
            ))}
          </div>
        ) : (
          <EmptyState title="No repair runs yet" description="Trigger the repair agent to populate per-source progress." />
        )}
      </Card>
    </div>
  );
}
