import { useEffect, useState, useCallback } from 'react';
import { api, Stats, AnalyzeStatus, SourceProgress } from '../api/client';

function StatCard({
  title,
  value,
  hint,
}: {
  title: string;
  value: number | string;
  hint?: string;
}) {
  return (
    <div className="bg-white rounded shadow-sm border p-4">
      <div className="text-xs uppercase tracking-wide text-gray-500">{title}</div>
      <div className="text-3xl font-bold mt-1">{value}</div>
      {hint ? <div className="text-xs text-gray-500 mt-1">{hint}</div> : null}
    </div>
  );
}

function SourceProgressCard({ row }: { row: SourceProgress }) {
  const pct =
    row.gaps_total > 0
      ? Math.min(100, Math.round((row.gaps_fixed / row.gaps_total) * 100))
      : 0;
  const done = row.gaps_total > 0 && row.gaps_fixed >= row.gaps_total;
  const barColor = done ? 'bg-green-500' : 'bg-blue-500';
  return (
    <div className="bg-white rounded border shadow-sm p-3 space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <div
          className="font-mono text-xs text-gray-700 truncate"
          title={row.source_id}
        >
          {row.source_id}
        </div>
        <div className="text-xs text-gray-500 shrink-0">
          {row.gaps_fixed}/{row.gaps_total}
        </div>
      </div>
      <div className="h-1.5 w-full rounded bg-gray-100 overflow-hidden">
        <div
          className={`h-full ${barColor}`}
          style={{ width: `${pct}%` }}
        />
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

export default function Dashboard() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [status, setStatus] = useState<AnalyzeStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [s, st] = await Promise.all([api.getStats(), api.getAnalyzeStatus()]);
      setStats(s);
      setStatus(st);
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

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4">
        <StatCard
          title="Source Points"
          value={stats?.total_source_points ?? '-'}
          hint="archdoc entry points"
        />
        <StatCard title="Files" value={stats?.total_files ?? '-'} />
        <StatCard title="Functions" value={stats?.total_functions ?? '-'} />
        <StatCard
          title="Resolved Calls"
          value={stats?.total_calls ?? '-'}
          hint={resolvedPct !== null ? `${resolvedPct}% resolved` : undefined}
        />
        <StatCard
          title="Unresolved GAPs"
          value={stats?.total_unresolved ?? '-'}
          hint="needs repair"
        />
      </div>

      <div className="bg-white rounded shadow-sm border p-4">
        <h2 className="font-semibold mb-3">Pipeline Actions</h2>
        <div className="flex flex-wrap gap-2">
          <button
            className="px-3 py-2 rounded bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-50"
            onClick={() => onAnalyze('full')}
            disabled={busy || status?.state === 'running'}
          >
            Run Full Analysis
          </button>
          <button
            className="px-3 py-2 rounded bg-blue-100 text-blue-800 text-sm hover:bg-blue-200 disabled:opacity-50"
            onClick={() => onAnalyze('incremental')}
            disabled={busy || status?.state === 'running'}
          >
            Run Incremental
          </button>
          <button
            className="px-3 py-2 rounded bg-amber-600 text-white text-sm hover:bg-amber-700 disabled:opacity-50"
            onClick={onRepair}
            disabled={busy || status?.state === 'repairing'}
          >
            Trigger Repair Agent
          </button>
          <button
            className="px-3 py-2 rounded border text-sm hover:bg-gray-50"
            onClick={refresh}
            disabled={busy}
          >
            Refresh
          </button>
        </div>
        <p className="text-xs text-gray-500 mt-3">
          Status polls every 2s. Triggering analysis is async on the server.
        </p>
      </div>

      <div className="bg-white rounded shadow-sm border p-4">
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
          <div className="text-sm text-gray-500">
            No repair runs yet. Trigger the repair agent to populate
            per-source progress.
          </div>
        )}
      </div>
    </div>
  );
}
