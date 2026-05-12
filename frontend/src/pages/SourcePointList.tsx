import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, SourceProgress, SourcePoint } from '../api/client';

// architecture.md §3 Repair Agent 进度文件契约 + §8 analyze/status
// schema：每个 source 点渲染一条 gaps_fixed/gaps_total mini bar + done/
// current:<gap>/idle 状态，与 Dashboard `SourceProgressCard` 共用视觉
// 语言。让审阅者在 SourcePointList 就能 triage "哪些已修完、哪些还在
// 跑、哪些未动"，不必切 Dashboard 找对应卡片（北极星 #5 状态透明度
// + #1 审阅耗时 + 候选优化方向 #4 进度与可观测性）。
function SourceProgressCell({ row }: { row: SourceProgress | undefined }) {
  if (!row || row.gaps_total === 0) {
    // No progress file yet (repair hasn't visited this source, or the
    // hook artefact hasn't been written). Render a muted dash so the
    // column stays readable without implying "0/0 = done".
    return <span className="text-gray-300 text-xs">—</span>;
  }
  const pct = Math.min(100, Math.round((row.gaps_fixed / row.gaps_total) * 100));
  const done = row.gaps_fixed >= row.gaps_total;
  const barColor = done ? 'bg-green-500' : 'bg-blue-500';
  return (
    <div className="min-w-[10rem] space-y-1">
      <div className="flex items-center gap-2 text-xs text-gray-600">
        <span className="shrink-0 tabular-nums">
          {row.gaps_fixed}/{row.gaps_total}
        </span>
        <span className="tabular-nums text-gray-400">{pct}%</span>
      </div>
      <div className="h-1.5 w-full rounded bg-gray-100 overflow-hidden">
        <div className={`h-full ${barColor}`} style={{ width: `${pct}%` }} />
      </div>
      <div className="text-[11px] text-gray-500 truncate">
        {done ? (
          <span className="text-green-700">done</span>
        ) : row.current_gap ? (
          <>
            current:{' '}
            <span className="font-mono text-gray-700">{row.current_gap}</span>
          </>
        ) : (
          <span className="text-gray-400">idle</span>
        )}
      </div>
    </div>
  );
}

export default function SourcePointList() {
  const [points, setPoints] = useState<SourcePoint[]>([]);
  const [progress, setProgress] = useState<Map<string, SourceProgress>>(
    new Map()
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState('');
  const [search, setSearch] = useState('');

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const data = await api.getSourcePoints();
        if (!cancelled) setPoints(data);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // 5s poll of /api/v1/analyze/status for per-source progress rows.
  // Mirrors App.tsx nav-chip cadence (browsing page, not operations
  // page — Dashboard polls at 2s). Non-blocking: a failed poll keeps
  // the last-known snapshot instead of blanking the column.
  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const st = await api.getAnalyzeStatus();
        if (cancelled) return;
        const next = new Map<string, SourceProgress>();
        for (const row of st.sources ?? []) {
          next.set(row.source_id, row);
        }
        setProgress(next);
      } catch {
        // silent — progress column is a surface affordance
      }
    };
    refresh();
    const id = setInterval(refresh, 5000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const kinds = useMemo(() => {
    const s = new Set<string>();
    points.forEach((p) => s.add(p.kind));
    return Array.from(s).sort();
  }, [points]);

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    return points.filter((p) => {
      if (kindFilter && p.kind !== kindFilter) return false;
      if (!term) return true;
      return (
        p.signature.toLowerCase().includes(term) ||
        p.file.toLowerCase().includes(term) ||
        p.module.toLowerCase().includes(term) ||
        p.reason.toLowerCase().includes(term)
      );
    });
  }, [points, kindFilter, search]);

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Source Points</h1>
        <div className="text-sm text-gray-500">
          {filtered.length} / {points.length} shown
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <input
          className="border rounded px-3 py-1 text-sm flex-1 min-w-[240px]"
          placeholder="Search signature, file, module, reason…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          className="border rounded px-3 py-1 text-sm"
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value)}
        >
          <option value="">All kinds</option>
          {kinds.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </div>

      {error ? (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded p-3 text-sm">
          {error}
        </div>
      ) : null}

      <div className="bg-white border rounded shadow-sm overflow-auto">
        <table className="min-w-full divide-y divide-gray-200 text-sm">
          <thead className="bg-gray-50 sticky top-0">
            <tr>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Kind</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Signature</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Module</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">File:Line</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Reason</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Progress</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {loading ? (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-center text-gray-500">
                  Loading…
                </td>
              </tr>
            ) : filtered.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-center text-gray-500">
                  No source points match.
                </td>
              </tr>
            ) : (
              filtered.map((p) => (
                <tr key={p.id} className="hover:bg-gray-50 align-top">
                  <td className="px-4 py-2">
                    <span className="inline-block px-2 py-0.5 rounded bg-blue-50 text-blue-700 text-xs">
                      {p.kind}
                    </span>
                  </td>
                  <td className="px-4 py-2 font-mono text-xs">{p.signature}</td>
                  <td className="px-4 py-2 text-gray-600">{p.module}</td>
                  <td className="px-4 py-2 font-mono text-xs text-gray-600">
                    {p.file}:{p.line}
                  </td>
                  <td className="px-4 py-2 text-gray-600">{p.reason}</td>
                  <td className="px-4 py-2">
                    <SourceProgressCell row={progress.get(p.id)} />
                  </td>
                  <td className="px-4 py-2">
                    <Link
                      to={`/graph?source=${encodeURIComponent(p.id)}`}
                      className="text-blue-600 hover:underline text-xs"
                    >
                      Browse →
                    </Link>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
