import { useEffect, useState, useCallback } from 'react';
import { api, Stats, AnalyzeStatus } from '../api/client';

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
    </div>
  );
}
