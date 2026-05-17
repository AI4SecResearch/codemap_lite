import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Bot, Clock, Play, ChevronDown, ChevronRight, FileCode } from 'lucide-react';
import { api, type AnalyzeStatus, type RepairLog } from '../api/client';
import { Button, Card, Badge, ProgressBar, Skeleton, EmptyState, Timestamp } from '../components/ui';

// --- Sub-components ---

function AuditLogEntry({ log }: { log: RepairLog }) {
  const [expanded, setExpanded] = useState(false);
  const [callerName, setCallerName] = useState<string | null>(null);
  const [calleeName, setCalleeName] = useState<string | null>(null);
  const Icon = expanded ? ChevronDown : ChevronRight;

  useEffect(() => {
    if (log.caller_id) {
      api.getFunction(log.caller_id)
        .then((fn) => setCallerName(fn.name || fn.signature?.split('(')[0] || null))
        .catch(() => {});
    }
    if (log.callee_id) {
      api.getFunction(log.callee_id)
        .then((fn) => setCalleeName(fn.name || fn.signature?.split('(')[0] || null))
        .catch(() => {});
    }
  }, [log.caller_id, log.callee_id]);

  return (
    <div className="border-l-2 border-blue-200 pl-4 py-3 hover:bg-gray-50 transition-colors rounded-r">
      <div
        className="flex items-start gap-2 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <Icon className="w-4 h-4 text-gray-400 mt-0.5 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs font-mono text-blue-700 bg-blue-50 rounded px-1.5 py-0.5" title={log.caller_id}>
              {callerName ?? log.caller_id.slice(0, 8)}
            </span>
            <span className="text-gray-400 text-xs">→</span>
            <span className="text-xs font-mono text-green-700 bg-green-50 rounded px-1.5 py-0.5" title={log.callee_id}>
              {calleeName ?? log.callee_id.slice(0, 8)}
            </span>
            <span className="text-xs text-gray-400">@</span>
            <span className="text-xs font-mono text-gray-600">{log.call_location}</span>
          </div>
          {log.reasoning_summary && (
            <p className="text-sm text-gray-700 mt-1 line-clamp-2">{log.reasoning_summary}</p>
          )}
          <div className="flex items-center gap-3 mt-1 text-xs text-gray-400">
            <Timestamp date={log.timestamp} />
            <span className="bg-gray-100 rounded px-1.5 py-0.5 text-gray-600">{log.repair_method}</span>
          </div>
        </div>
      </div>

      {expanded && log.llm_response && (
        <div className="mt-3 ml-6">
          <pre className="text-xs font-mono bg-gray-900 text-gray-100 rounded-lg p-3 overflow-x-auto max-h-[300px] overflow-y-auto whitespace-pre-wrap">
            {log.llm_response}
          </pre>
        </div>
      )}
    </div>
  );
}

// --- Main page ---

export default function RepairActivity() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [status, setStatus] = useState<AnalyzeStatus | null>(null);
  const [logs, setLogs] = useState<RepairLog[]>([]);
  const [totalLogs, setTotalLogs] = useState(0);
  const [loading, setLoading] = useState(true);
  const [logsPage, setLogsPage] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const filterSource = searchParams.get('source');
  const LOGS_PER_PAGE = 20;

  const refresh = useCallback(async () => {
    try {
      const [st, rl] = await Promise.all([
        api.getAnalyzeStatus(),
        api.getRepairLogs({
          limit: LOGS_PER_PAGE,
          offset: logsPage * LOGS_PER_PAGE,
          ...(filterSource ? { source_reachable: filterSource } : {}),
        }),
      ]);
      setStatus(st);
      setLogs(rl.items);
      setTotalLogs(rl.total);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [logsPage, filterSource]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 3000);
    return () => clearInterval(id);
  }, [refresh]);

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

  const sources = status?.sources ?? [];
  const running = sources.filter((s) => s.state === 'running' || s.state === 'gate_checking');
  const completed = sources.filter((s) => s.state === 'succeeded');
  const failed = sources.filter((s) => s.state === 'failed');
  const waiting = sources.filter((s) => !s.state || s.state === 'waiting');

  // Resolve source_id hashes to function names for display
  const [sourceNames, setSourceNames] = useState<Record<string, string>>({});
  useEffect(() => {
    const ids = sources.map((s) => s.source_id).filter(Boolean);
    if (ids.length === 0) return;
    let cancelled = false;
    Promise.all(
      ids.map((id) =>
        api.getFunction(id)
          .then((fn) => [id, fn.name || fn.signature?.split('(')[0] || id] as [string, string])
          .catch(() => [id, id] as [string, string])
      )
    ).then((pairs) => {
      if (cancelled) return;
      setSourceNames(Object.fromEntries(pairs));
    });
    return () => { cancelled = true; };
  }, [sources.length]);

  const getSourceDisplayName = (id: string) => sourceNames[id] || id;

  const overallPct = status?.progress != null ? Math.round(status.progress * 100) : 0;

  if (loading) {
    return (
      <div className="p-6 space-y-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Bot className="w-6 h-6 text-blue-600" />
          <h1 className="text-2xl font-bold">Repair Activity</h1>
        </div>
        <div className="flex items-center gap-3">
          <span className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium ${
            status?.state === 'repairing'
              ? 'bg-blue-100 text-blue-700'
              : status?.state === 'running'
              ? 'bg-amber-100 text-amber-700'
              : 'bg-gray-100 text-gray-700'
          }`}>
            <span className={`w-2 h-2 rounded-full ${
              status?.state === 'repairing' ? 'bg-blue-500 animate-pulse' : 'bg-gray-400'
            }`} />
            {status?.state ?? 'idle'}
          </span>
          <Button onClick={onRepair} loading={busy} icon={<Play className="w-4 h-4" />}>
            Start Repair
          </Button>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm">
          {error}
        </div>
      )}

      {/* Source filter */}
      {filterSource && (
        <div className="flex items-center gap-2 rounded-lg border border-blue-200 bg-blue-50 p-2 text-sm text-blue-800">
          <span>Filtered by source:</span>
          <Badge tone="blue">{getSourceDisplayName(filterSource)}</Badge>
          <button onClick={() => setSearchParams({}, { replace: true })} className="ml-auto text-xs underline opacity-70 hover:opacity-100">Clear</button>
        </div>
      )}
      {!filterSource && sources.length > 0 && (
        <select
          className="text-sm border rounded-lg px-3 py-1.5 bg-white"
          value=""
          onChange={(e) => { if (e.target.value) setSearchParams({ source: e.target.value }, { replace: true }); }}
        >
          <option value="">Filter by source…</option>
          {sources.map((s) => <option key={s.source_id} value={s.source_id}>{getSourceDisplayName(s.source_id)}</option>)}
        </select>
      )}

      {/* Overall progress */}
      <Card className="p-5">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold text-gray-800">Overall Progress</h2>
          <div className="flex items-center gap-4 text-sm text-gray-600">
            <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-blue-500 animate-pulse" /> {running.length} running</span>
            <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-green-500" /> {completed.length} done</span>
            <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-red-500" /> {failed.length} failed</span>
            <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-gray-300" /> {waiting.length} waiting</span>
          </div>
        </div>
        <ProgressBar value={overallPct / 100} label={`${overallPct}%`} />
        {status?.started_at && (
          <div className="mt-2 text-xs text-gray-500">
            Started <Timestamp date={status.started_at} />
            {status.completed_at && <> · Completed <Timestamp date={status.completed_at} /></>}
          </div>
        )}
      </Card>

      {/* Per-source progress is shown in the Sources page — no need to duplicate here */}

      {sources.length === 0 && !logs.length && (
        <EmptyState
          title="No active repair sessions"
          description="Click 'Start Repair' to begin resolving indirect call GAPs with the LLM agent."
          icon={<Bot className="w-10 h-10 text-gray-300" />}
        />
      )}

      {/* Audit log timeline */}
      <Card className="p-5">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Clock className="w-5 h-5 text-gray-500" />
            <h2 className="font-semibold text-gray-800">Repair Audit Log</h2>
          </div>
          <span className="text-xs text-gray-500">{totalLogs} total entries</span>
        </div>

        {logs.length > 0 ? (
          <div className="space-y-1">
            {logs.map((log) => (
              <AuditLogEntry key={log.id} log={log} />
            ))}
          </div>
        ) : (
          <EmptyState
            title="No repair logs yet"
            description="Repair logs appear here as the agent resolves indirect call GAPs."
            icon={<FileCode className="w-10 h-10 text-gray-300" />}
          />
        )}

        {/* Pagination */}
        {totalLogs > LOGS_PER_PAGE && (
          <div className="flex items-center justify-between mt-4 pt-4 border-t border-gray-100">
            <Button
              variant="ghost"
              size="sm"
              disabled={logsPage === 0}
              onClick={() => setLogsPage((p) => Math.max(0, p - 1))}
            >
              Previous
            </Button>
            <span className="text-xs text-gray-500">
              Page {logsPage + 1} of {Math.ceil(totalLogs / LOGS_PER_PAGE)}
            </span>
            <Button
              variant="ghost"
              size="sm"
              disabled={(logsPage + 1) * LOGS_PER_PAGE >= totalLogs}
              onClick={() => setLogsPage((p) => p + 1)}
            >
              Next
            </Button>
          </div>
        )}
      </Card>
    </div>
  );
}
