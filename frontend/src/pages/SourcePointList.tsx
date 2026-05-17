import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { RefreshCw, Wrench, ChevronDown, ChevronRight, CheckCircle2, XCircle, AlertTriangle, Clock, Plus, FileCode, Network } from 'lucide-react';
import { api, type SourcePoint, type SourceProgress, type RepairLog, type UnresolvedCall, type AnalyzeStatus } from '../api/client';
import { Button, Badge, Card, ProgressBar, SearchInput, Skeleton, SkeletonCard, ConfirmDialog, EmptyState, Timestamp } from '../components/ui';

// --- Tone maps ---
const STATUS_TONE: Record<string, 'gray' | 'blue' | 'green' | 'amber' | 'red' | 'sky'> = {
  pending: 'amber',
  unresolvable: 'red',
  running: 'blue',
  complete: 'green',
  partial_complete: 'sky',
};

const CATEGORY_TONE: Record<string, 'amber' | 'red' | 'fuchsia' | 'orange' | 'sky'> = {
  gate_failed: 'amber',
  agent_error: 'red',
  subprocess_crash: 'fuchsia',
  subprocess_timeout: 'orange',
  agent_exited_without_edge: 'sky',
};

// Tailwind JIT needs full class strings — dynamic `bg-${tone}-50` won't work.
const CATEGORY_CLASSES: Record<string, string> = {
  amber: 'bg-amber-50 text-amber-800 border-amber-200',
  red: 'bg-red-50 text-red-800 border-red-200',
  fuchsia: 'bg-fuchsia-50 text-fuchsia-800 border-fuchsia-200',
  orange: 'bg-orange-50 text-orange-800 border-orange-200',
  sky: 'bg-sky-50 text-sky-800 border-sky-200',
};

// --- Sub-components ---

function CodeBlock({ content, highlightLine }: { content: string; highlightLine?: number }) {
  const lines = content.split('\n');
  return (
    <pre className="text-xs font-mono bg-gray-900 text-gray-100 rounded-lg p-3 overflow-x-auto max-h-[400px] overflow-y-auto">
      {lines.map((line, i) => (
        <div key={i} className={highlightLine != null && i === highlightLine ? 'bg-yellow-900/40' : ''}>
          {line}
        </div>
      ))}
    </pre>
  );
}

function RepairLogCard({ log, onMarkCorrect, onMarkWrong }: {
  log: RepairLog; onMarkCorrect: () => void; onMarkWrong: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [callSiteCode, setCallSiteCode] = useState<string | null>(null);
  const [calleeCode, setCalleeCode] = useState<string | null>(null);
  const [showCallee, setShowCallee] = useState(false);
  const [showFullResponse, setShowFullResponse] = useState(false);
  const [callSiteLine, setCallSiteLine] = useState<number | null>(null);
  const [reviewed, setReviewed] = useState<'correct' | 'wrong' | null>(null);
  const [callerName, setCallerName] = useState<string | null>(null);
  const [calleeName, setCalleeName] = useState<string | null>(null);

  // Resolve hash IDs to function names
  useEffect(() => {
    if (log.caller_id) {
      api.getFunction(log.caller_id)
        .then((fn) => setCallerName(fn.name || fn.signature?.split('(')[0] || log.caller_id))
        .catch(() => setCallerName(null));
    }
    if (log.callee_id) {
      api.getFunction(log.callee_id)
        .then((fn) => setCalleeName(fn.name || fn.signature?.split('(')[0] || log.callee_id))
        .catch(() => setCalleeName(null));
    }
  }, [log.caller_id, log.callee_id]);

  useEffect(() => {
    if (!expanded) return;
    const match = log.call_location?.match(/^(.+):(\d+)$/);
    if (match) {
      const [, file, lineStr] = match;
      const line = parseInt(lineStr, 10);
      const start = Math.max(1, line - 3);
      const end = line + 3;
      setCallSiteLine(line - start);
      api.getSourceCode(file, start, end)
        .then((r) => setCallSiteCode(r.content))
        .catch(() => setCallSiteCode(null));
    }
  }, [expanded, log.call_location]);

  useEffect(() => {
    if (!showCallee || calleeCode !== null) return;
    if (!log.callee_id) return;
    api.getFunction(log.callee_id)
      .then((fn: any) => {
        if (fn.file_path && fn.start_line && fn.end_line) {
          return api.getSourceCode(fn.file_path, fn.start_line, fn.end_line);
        }
        return null;
      })
      .then((r: any) => setCalleeCode(r?.content ?? '(source unavailable)'))
      .catch(() => setCalleeCode('(failed to load)'));
  }, [showCallee, calleeCode, log.callee_id]);

  const displayCaller = callerName ?? '…';
  const displayCallee = calleeName ?? '…';

  return (
    <Card className="animate-fade-in overflow-hidden">
      {/* Collapsed header — always visible */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-4 py-3 text-left hover:bg-gray-50 transition-colors"
      >
        {expanded ? <ChevronDown className="w-4 h-4 text-gray-400 shrink-0" /> : <ChevronRight className="w-4 h-4 text-gray-400 shrink-0" />}
        <span className="text-sm font-medium text-gray-900 truncate">
          {displayCaller} → {displayCallee}
        </span>
        <span className="text-xs text-gray-500 shrink-0">{log.call_location}</span>
        <span className="ml-auto shrink-0 whitespace-nowrap"><Timestamp date={log.timestamp} /></span>
      </button>

      {/* Expanded body */}
      {expanded && (
        <div className="px-4 pb-4 space-y-3 border-t border-gray-100 pt-3">
          {callSiteCode && (
            <div>
              <div className="text-xs text-gray-500 mb-1">调用位置:</div>
              <CodeBlock content={callSiteCode} highlightLine={callSiteLine ?? undefined} />
            </div>
          )}
          <div>
            <button onClick={() => setShowCallee(!showCallee)} className="text-xs text-blue-600 hover:underline">
              {showCallee ? '▼ 隐藏被调用函数' : '▶ 查看被调用函数'}
            </button>
            {showCallee && calleeCode && <div className="mt-1"><CodeBlock content={calleeCode} /></div>}
          </div>
          {log.reasoning_summary && (
            <div className="text-sm text-gray-700 bg-blue-50 border border-blue-100 rounded-lg p-3">
              <span className="font-medium text-blue-700">推理: </span>{log.reasoning_summary}
            </div>
          )}
          {log.llm_response && (
            <div>
              <button onClick={() => setShowFullResponse(!showFullResponse)} className="text-xs text-gray-500 hover:underline">
                {showFullResponse ? '▼ 隐藏完整响应' : '▶ 查看完整 LLM 响应'}
              </button>
              {showFullResponse && (
                <pre className="mt-1 text-xs bg-gray-50 rounded-lg p-3 overflow-auto max-h-[300px] whitespace-pre-wrap border">{log.llm_response}</pre>
              )}
            </div>
          )}
          <div className="flex gap-2 pt-1">
            {reviewed === 'correct' ? (
              <Badge tone="green" icon={<CheckCircle2 className="w-3 h-3" />}>已标记正确</Badge>
            ) : reviewed === 'wrong' ? (
              <Badge tone="red" icon={<XCircle className="w-3 h-3" />}>已标记错误</Badge>
            ) : (
              <>
                <Button variant="secondary" size="sm" icon={<CheckCircle2 className="w-3.5 h-3.5" />} onClick={() => { setReviewed('correct'); onMarkCorrect(); }}>正确</Button>
                <Button variant="secondary" size="sm" icon={<XCircle className="w-3.5 h-3.5" />} onClick={() => { setReviewed('wrong'); onMarkWrong(); }}>错误</Button>
              </>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}

// --- PLACEHOLDER_SOURCEDETAIL ---

function UnresolvedGapCard({ uc, tone, onResolve, resolveForm }: {
  uc: UnresolvedCall;
  tone: string | undefined;
  onResolve: () => void;
  resolveForm: React.ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const [callSiteCode, setCallSiteCode] = useState<string | null>(null);
  const [callSiteLine, setCallSiteLine] = useState<number | null>(null);
  const [callerName, setCallerName] = useState<string | null>(null);

  useEffect(() => {
    if (uc.caller_id) {
      api.getFunction(uc.caller_id)
        .then((fn) => setCallerName(fn.name || fn.signature?.split('(')[0] || null))
        .catch(() => {});
    }
  }, [uc.caller_id]);

  useEffect(() => {
    if (!expanded || callSiteCode !== null) return;
    if (uc.call_file && uc.call_line) {
      const start = Math.max(1, uc.call_line - 3);
      const end = uc.call_line + 3;
      setCallSiteLine(uc.call_line - start);
      api.getSourceCode(uc.call_file, start, end)
        .then((r) => setCallSiteCode(r.content))
        .catch(() => setCallSiteCode(null));
    }
  }, [expanded, callSiteCode, uc.call_file, uc.call_line]);

  return (
    <Card className="animate-fade-in overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-4 py-3 text-left hover:bg-gray-50 transition-colors"
      >
        {expanded ? <ChevronDown className="w-4 h-4 text-gray-400 shrink-0" /> : <ChevronRight className="w-4 h-4 text-gray-400 shrink-0" />}
        <span className="text-sm font-medium text-gray-900 truncate">
          {callerName ?? uc.caller_id?.slice(0, 8) ?? '?'} → {uc.call_expression || '(indirect)'}
        </span>
        <span className="text-xs text-gray-500 shrink-0">{uc.call_file}:{uc.call_line}</span>
        <span className="ml-auto flex items-center gap-2">
          <Badge tone={STATUS_TONE[uc.status ?? 'pending'] ?? 'gray'}>{uc.status ?? 'pending'}</Badge>
          <Badge tone="gray" icon={<Clock className="w-3 h-3" />}>retry {uc.retry_count ?? 0}/3</Badge>
        </span>
      </button>

      {expanded && (
        <div className="px-4 pb-4 space-y-3 border-t border-gray-100 pt-3">
          {uc.last_attempt_reason
            ? <div className={`px-2 py-1 rounded border text-xs ${CATEGORY_CLASSES[tone ?? ''] ?? 'bg-gray-50 text-gray-600 border-gray-200'}`}>{uc.last_attempt_reason}</div>
            : <div className="text-xs text-gray-400 italic">{(uc.retry_count ?? 0) === 0 ? 'Not yet attempted — click Repair to start' : 'No failure reason recorded'}</div>
          }

          {callSiteCode && (
            <div>
              <div className="text-xs text-gray-500 mb-1">调用位置:</div>
              <CodeBlock content={callSiteCode} highlightLine={callSiteLine ?? undefined} />
            </div>
          )}

          {uc.source_code_snippet && !callSiteCode && (
            <div>
              <div className="text-xs text-gray-500 mb-1">代码片段:</div>
              <pre className="bg-gray-900 text-gray-100 rounded-lg p-2 overflow-x-auto text-[11px]">{uc.source_code_snippet}</pre>
            </div>
          )}

          {uc.var_name && (
            <div className="text-xs text-gray-600">
              <span className="font-medium">变量:</span> <code className="bg-gray-100 px-1 rounded">{uc.var_name}</code>
              {uc.var_type && <> : <code className="bg-gray-100 px-1 rounded">{uc.var_type}</code></>}
            </div>
          )}

          {uc.candidates && uc.candidates.length > 0 && (
            <div className="text-xs text-gray-600">
              <span className="font-medium">候选目标:</span> {uc.candidates.join(', ')}
            </div>
          )}

          <div className="flex items-center gap-2 pt-1">
            <button onClick={(e) => { e.stopPropagation(); onResolve(); }}>
              <Badge tone="green" icon={<Plus className="w-3 h-3" />}>Resolve</Badge>
            </button>
          </div>

          {resolveForm}
        </div>
      )}
    </Card>
  );
}

const STATUS_TOOLTIP: Record<string, string> = {
  pending: '等待修复 — agent 尚未处理此 GAP',
  unresolvable: '已放弃 — 重试 3 次后 agent 仍无法解决',
  running: '修复中 — agent 正在分析',
  complete: '已完成 — 所有 GAP 已解决',
  partial_complete: '部分完成 — 仍有未解决 GAP',
};

function AgentTerminal({ sourceId, active, onFinished }: { sourceId: string; active: boolean; onFinished?: () => void }) {
  const [lines, setLines] = useState<string[]>([]);
  const [attempt, setAttempt] = useState(0);
  const [finished, setFinished] = useState(false);
  const [hasLogs, setHasLogs] = useState(false);
  const termRef = useRef<HTMLDivElement>(null);

  // Always fetch once on mount to check for existing logs
  useEffect(() => {
    let cancelled = false;
    const stripAnsi = (s: string) => s.replace(/\x1b\[[0-9;]*m/g, '');
    api.getLiveLog(sourceId, 30)
      .then((data) => {
        if (cancelled) return;
        if (data.lines.length > 0) {
          setLines(data.lines.map(stripAnsi));
          setAttempt(data.attempt);
          setFinished(data.finished);
          setHasLogs(true);
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [sourceId]);

  // Poll when actively repairing
  useEffect(() => {
    if (!active || finished) return;
    let cancelled = false;
    const stripAnsi = (s: string) => s.replace(/\x1b\[[0-9;]*m/g, '');
    const poll = async () => {
      try {
        const data = await api.getLiveLog(sourceId, 30);
        if (cancelled) return;
        setLines(data.lines.map(stripAnsi));
        setAttempt(data.attempt);
        setHasLogs(data.lines.length > 0);
        if (data.finished && !finished) {
          setFinished(true);
          onFinished?.();
        }
      } catch { /* silent */ }
    };
    const id = setInterval(poll, 2000);
    return () => { cancelled = true; clearInterval(id); };
  }, [sourceId, active, finished, onFinished]);

  useEffect(() => {
    if (termRef.current) termRef.current.scrollTop = termRef.current.scrollHeight;
  }, [lines]);

  if (!hasLogs && !active) return null;

  return (
    <div className="mt-3 rounded-lg overflow-hidden border border-gray-700">
      <div className="flex items-center gap-2 px-3 py-1.5 bg-gray-800 text-xs text-gray-300">
        {!finished && <span className="w-2 h-2 rounded-full bg-green-400 animate-pulse" />}
        <span>{finished ? `Attempt ${attempt} — finished` : `Agent running… (attempt ${attempt})`}</span>
      </div>
      <div ref={termRef} className="bg-gray-900 text-gray-100 font-mono text-xs p-3 max-h-[240px] overflow-y-auto">
        {lines.map((line, i) => <div key={i}>{line || '\u00A0'}</div>)}
      </div>
      {finished && (
        <div className="px-3 py-2 bg-gray-800 text-xs">
          <Link to={`/repairs?source=${encodeURIComponent(sourceId)}`} className="text-blue-400 hover:underline">
            查看完整推理日志 →
          </Link>
        </div>
      )}
    </div>
  );
}

function SourceDetail({ source }: { source: SourcePoint }) {
  const [repairLogs, setRepairLogs] = useState<RepairLog[]>([]);
  const [unresolvedCalls, setUnresolvedCalls] = useState<UnresolvedCall[]>([]);
  const [loadingLogs, setLoadingLogs] = useState(true);
  const [loadingUCs, setLoadingUCs] = useState(true);
  const [repairing, setRepairing] = useState(false);
  const [feedbackBanner, setFeedbackBanner] = useState<{ pattern: string } | null>(null);
  const [resolveGapId, setResolveGapId] = useState<string | null>(null);
  const [addEdgeForm, setAddEdgeForm] = useState({ callee_id: '', call_type: 'indirect' });
  const [addingEdge, setAddingEdge] = useState(false);
  const [addEdgeError, setAddEdgeError] = useState<string | null>(null);
  const [logsCollapsed, setLogsCollapsed] = useState(true);
  const [gapsCollapsed, setGapsCollapsed] = useState(true);
  const [confirmWrong, setConfirmWrong] = useState<RepairLog | null>(null);

  useEffect(() => {
    const fid = source.function_id ?? source.id;
    // Use source_reachable to get ALL repair logs from the entire call subgraph
    // (depth=1 and deeper), not just direct caller matches.
    api.getRepairLogs({ source_reachable: fid, limit: 500 })
      .then((r) => setRepairLogs(r.items))
      .catch(() => {})
      .finally(() => setLoadingLogs(false));
    api.listUnresolved({ caller: fid })
      .then((r) => setUnresolvedCalls(r.items))
      .catch(() => {})
      .finally(() => setLoadingUCs(false));
  }, [source.function_id, source.id]);

  const refreshData = async () => {
    const fid = source.function_id ?? source.id;
    const [r, u] = await Promise.all([
      api.getRepairLogs({ source_reachable: fid, limit: 500 }),
      api.listUnresolved({ caller: fid }),
    ]);
    setRepairLogs(r.items);
    setUnresolvedCalls(u.items);
  };

  const sourceNeoId = source.function_id || source.id;

  const handleRepairSource = async () => {
    setRepairing(true);
    try { await api.triggerRepair([sourceNeoId]); } catch (e) { setAddEdgeError(e instanceof Error ? e.message : String(e)); setRepairing(false); }
  };

  const handleMarkCorrect = async (log: RepairLog) => {
    try {
      await api.createReview({ caller_id: log.caller_id, callee_id: log.callee_id,
        call_file: log.call_location?.split(':')[0] ?? '', call_line: parseInt(log.call_location?.split(':')[1] ?? '0', 10), verdict: 'correct' });
      await refreshData();
    } catch (e) { setAddEdgeError(e instanceof Error ? e.message : String(e)); }
  };

  const handleMarkWrong = async (log: RepairLog) => {
    const callFile = log.call_location?.split(':')[0] ?? '';
    const callLine = parseInt(log.call_location?.split(':')[1] ?? '0', 10);
    try {
      await api.createReview({ caller_id: log.caller_id, callee_id: log.callee_id, call_file: callFile, call_line: callLine, verdict: 'incorrect' });
      const pattern = `${callFile}:${callLine} → ${log.callee_id}`;
      const result = await api.createFeedback({ call_context: `${log.caller_id} @ ${log.call_location}`, wrong_target: log.callee_id, correct_target: '', pattern });
      await api.deleteEdge({ caller_id: log.caller_id, callee_id: log.callee_id, call_file: callFile, call_line: callLine });
      setFeedbackBanner({ pattern: result.pattern ?? pattern });
      await refreshData();
    } catch { /* handled by UI state */ }
    setConfirmWrong(null);
  };

  const handleResolveGap = async (uc: UnresolvedCall) => {
    if (!addEdgeForm.callee_id) return;
    setAddingEdge(true);
    setAddEdgeError(null);
    try {
      await api.createEdge({
        caller_id: source.function_id ?? source.id,
        callee_id: addEdgeForm.callee_id,
        call_file: uc.call_file,
        call_line: uc.call_line,
        call_type: addEdgeForm.call_type,
        resolved_by: 'context',
      });
      setResolveGapId(null);
      setAddEdgeForm({ callee_id: '', call_type: 'indirect' });
      await refreshData();
    } catch (e) {
      setAddEdgeError(e instanceof Error ? e.message : String(e));
    }
    setAddingEdge(false);
  };

  return (
    <div className="px-6 py-4 bg-gray-50/50 border-t space-y-4 animate-slide-down">
      {/* Quick summary */}
      <div className="flex items-center gap-4 text-xs text-gray-600">
        <span className="flex items-center gap-1"><CheckCircle2 className="w-3.5 h-3.5 text-green-600" />{repairLogs.length} repaired</span>
        <span className="flex items-center gap-1"><AlertTriangle className="w-3.5 h-3.5 text-amber-600" />{unresolvedCalls.length} unresolved</span>
        {repairLogs.length + unresolvedCalls.length > 0 && (
          <span className="text-gray-400">({Math.round(repairLogs.length / (repairLogs.length + unresolvedCalls.length) * 100)}% resolved)</span>
        )}
      </div>

      {/* Confirm dialog for mark-wrong */}
      <ConfirmDialog
        open={!!confirmWrong}
        title="Mark edge as incorrect?"
        description="This will delete the edge, generate a counter-example, and trigger re-repair for this source point."
        confirmLabel="Mark Wrong"
        variant="danger"
        onConfirm={() => confirmWrong && handleMarkWrong(confirmWrong)}
        onCancel={() => setConfirmWrong(null)}
      />

      {feedbackBanner && (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-green-200 bg-green-50 p-3 text-sm text-green-800 animate-fade-in">
          <span>
            Counter-example saved.{' '}
            <Link to={`/feedback?pattern=${encodeURIComponent(feedbackBanner.pattern)}`} className="underline font-medium">View in Feedback Log</Link>
          </span>
          <button className="text-xs opacity-70 hover:opacity-100" onClick={() => setFeedbackBanner(null)}>dismiss</button>
        </div>
      )}

      {/* Repair Results (collapsible) */}
      <section>
        <button onClick={() => setLogsCollapsed(!logsCollapsed)} className="flex items-center gap-2 text-sm font-semibold text-gray-700 mb-2 hover:text-gray-900 transition-colors">
          {logsCollapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          <CheckCircle2 className="w-4 h-4 text-green-600" />
          Repair Results ({repairLogs.length})
        </button>
        {!logsCollapsed && (
          loadingLogs ? <div className="space-y-3"><SkeletonCard /><SkeletonCard /></div>
          : repairLogs.length === 0 ? <EmptyState title="No repair logs yet" description="Run repair to generate results" />
          : <div className="space-y-3">{repairLogs.map((l) => (
              <RepairLogCard key={l.id} log={l} onMarkCorrect={() => handleMarkCorrect(l)} onMarkWrong={() => setConfirmWrong(l)} />
            ))}</div>
        )}
      </section>

      {/* Unresolved GAPs (collapsible) */}
      <section>
        <button onClick={() => setGapsCollapsed(!gapsCollapsed)} className="flex items-center gap-2 text-sm font-semibold text-gray-700 mb-2 hover:text-gray-900 transition-colors">
          {gapsCollapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          <AlertTriangle className="w-4 h-4 text-amber-600" />
          Unresolved GAPs ({unresolvedCalls.length})
          {!gapsCollapsed && unresolvedCalls.length > 0 && (
            <span className="text-xs text-gray-500 ml-2 font-normal">
              {Object.entries(
                unresolvedCalls.reduce<Record<string, number>>((acc, uc) => {
                  const cat = uc.last_attempt_reason?.split(':')[0] ?? 'none';
                  acc[cat] = (acc[cat] ?? 0) + 1;
                  return acc;
                }, {})
              ).map(([cat, n]) => `${n} ${cat}`).join(' · ')}
            </span>
          )}
        </button>
        {!gapsCollapsed && (
          loadingUCs ? <div className="space-y-2"><Skeleton className="h-16 w-full" /><Skeleton className="h-16 w-full" /></div>
          : unresolvedCalls.length === 0 ? <EmptyState title="All resolved" description="No unresolved gaps for this source" />
          : <div className="space-y-2">{unresolvedCalls.map((uc) => {
              const cat = uc.last_attempt_reason?.split(':')[0] ?? '';
              const tone = CATEGORY_TONE[cat] ?? undefined;
              return (
                <UnresolvedGapCard
                  key={uc.id ?? `${uc.call_file}:${uc.call_line}`}
                  uc={uc}
                  tone={tone}
                  onResolve={() => { setResolveGapId(uc.id ?? `${uc.call_file}:${uc.call_line}`); setAddEdgeForm({ callee_id: '', call_type: uc.call_type || 'indirect' }); }}
                  resolveForm={resolveGapId === (uc.id ?? `${uc.call_file}:${uc.call_line}`) ? (
                    <div className="mt-3 border-t pt-3 space-y-2 animate-fade-in">
                      <div className="text-xs text-gray-500 font-medium">Resolve: {uc.call_file}:{uc.call_line}</div>
                      <div className="flex gap-2">
                        <input className="flex-1 border border-gray-300 rounded-lg px-3 py-1.5 text-xs focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none transition-all" placeholder="Callee function ID (target)" value={addEdgeForm.callee_id} onChange={(e) => setAddEdgeForm({ ...addEdgeForm, callee_id: e.target.value })} />
                        <Button size="sm" loading={addingEdge} disabled={!addEdgeForm.callee_id} onClick={() => handleResolveGap(uc)}>Add Edge</Button>
                        <Button variant="ghost" size="sm" onClick={() => setResolveGapId(null)}>Cancel</Button>
                      </div>
                      {addEdgeError && <div className="text-xs text-red-600 mt-1">{addEdgeError}</div>}
                    </div>
                  ) : null}
                />
              );
            })}
          </div>
        )}
      </section>

      {/* Agent terminal — shows when repair is running or has logs */}
      <AgentTerminal sourceId={sourceNeoId} active={repairing} onFinished={() => { setRepairing(false); refreshData(); }} />

      <div className="pt-2 flex items-center gap-2">
        <Button size="sm" onClick={handleRepairSource} loading={repairing} icon={<Wrench className="w-3.5 h-3.5" />}>
          Repair
        </Button>
        <Link to={`/graph?function=${encodeURIComponent(sourceNeoId)}`}>
          <Button variant="ghost" size="sm" icon={<Network className="w-3.5 h-3.5" />}>
            Call Graph
          </Button>
        </Link>
        <Link to={`/repairs?source=${encodeURIComponent(sourceNeoId)}`}>
          <Button variant="ghost" size="sm" icon={<FileCode className="w-3.5 h-3.5" />}>
            修复日志
          </Button>
        </Link>
      </div>
    </div>
  );
}

// --- PLACEHOLDER_MAIN ---

export default function SourcePointList() {
  const [points, setPoints] = useState<SourcePoint[]>([]);
  const [progress, setProgress] = useState<Map<string, SourceProgress>>(new Map());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [fetching, setFetching] = useState(false);
  const [repairingAll, setRepairingAll] = useState(false);
  const [stats, setStats] = useState<{ total_unresolved?: number; total_repair_logs?: number; total_feedback?: number } | null>(null);
  const [analyzeState, setAnalyzeState] = useState<AnalyzeStatus | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [categoryCallers, setCategoryCallers] = useState<Set<string> | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [focusIdx, setFocusIdx] = useState(0);
  const [confirmRepairAll, setConfirmRepairAll] = useState(false);
  const [viewMode, setViewMode] = useState<'list' | 'module'>('module');
  const listRef = useRef<HTMLDivElement>(null);

  const filterStatus = searchParams.get('status');
  const filterCaller = searchParams.get('caller');
  const filterCategory = searchParams.get('category');
  const hasFilter = !!(filterStatus || filterCaller || filterCategory);

  const loadPoints = useCallback(async () => {
    try {
      const data = await api.getSourcePoints();
      setPoints(data.items);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadPoints(); }, [loadPoints]);

  useEffect(() => {
    if (!filterCategory) { setCategoryCallers(null); return; }
    let cancelled = false;
    api.listUnresolved({ category: filterCategory, limit: 500 })
      .then((r) => {
        if (cancelled) return;
        const callers = new Set<string>();
        for (const uc of r.items) if (uc.caller_id) callers.add(uc.caller_id);
        setCategoryCallers(callers);
      })
      .catch(() => { if (!cancelled) setCategoryCallers(new Set()); });
    return () => { cancelled = true; };
  }, [filterCategory]);

  useEffect(() => {
    const refresh = async () => {
      try {
        const st = await api.getAnalyzeStatus();
        setAnalyzeState(st);
        const next = new Map<string, SourceProgress>();
        for (const row of st.sources ?? []) next.set(row.source_id, row);
        setProgress(next);
        if (st.error && !error) setError(`Analysis failed: ${st.error}`);
      } catch { /* silent */ }
      try { const s = await api.getStats(); setStats(s); } catch { /* silent */ }
      setLastRefresh(new Date());
    };
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, []);

  const handleFetchSources = async () => {
    setFetching(true);
    setError(null);
    try {
      await api.triggerAnalyze('incremental');
      await loadPoints();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setFetching(false);
  };

  const handleRepairAll = async () => {
    setRepairingAll(true);
    setError(null);
    try { await api.triggerRepair(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    setRepairingAll(false);
  };

  const handleRepairSelected = async () => {
    setRepairingAll(true);
    try { await api.triggerRepair([...selected]); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    setRepairingAll(false);
  };

  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    points.forEach((p) => { counts[p.status ?? 'unknown'] = (counts[p.status ?? 'unknown'] ?? 0) + 1; });
    return counts;
  }, [points]);

  const filteredPoints = useMemo(() => {
    let result = points;
    if (filterStatus) result = result.filter((p) => (p.status ?? 'pending') === filterStatus);
    if (filterCaller) result = result.filter((p) => (p.function_id ?? p.id) === filterCaller);
    if (filterCategory && categoryCallers) result = result.filter((p) => categoryCallers.has(p.function_id ?? p.id));
    if (search) {
      const q = search.toLowerCase();
      result = result.filter((p) => p.signature.toLowerCase().includes(q) || p.file.toLowerCase().includes(q) || p.module.toLowerCase().includes(q));
    }
    return result;
  }, [points, filterStatus, filterCaller, filterCategory, categoryCallers, search]);

  const groupedByModule = useMemo(() => {
    if (viewMode !== 'module') return null;
    const groups = new Map<string, SourcePoint[]>();
    filteredPoints.forEach((p) => {
      const mod = p.module || '(unknown)';
      if (!groups.has(mod)) groups.set(mod, []);
      groups.get(mod)!.push(p);
    });
    return groups;
  }, [filteredPoints, viewMode]);

  // Keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      if (e.key === 'j') { e.preventDefault(); setFocusIdx((i) => Math.min(i + 1, filteredPoints.length - 1)); }
      else if (e.key === 'k') { e.preventDefault(); setFocusIdx((i) => Math.max(i - 1, 0)); }
      else if (e.key === 'Enter') { e.preventDefault(); const p = filteredPoints[focusIdx]; if (p) setExpandedId(expandedId === p.id ? null : p.id); }
      else if (e.key === ' ') { e.preventDefault(); const p = filteredPoints[focusIdx]; if (p) toggleSelect(p.id); }
      else if (e.key === 'r') { e.preventDefault(); const p = filteredPoints[focusIdx]; if (p) { api.triggerRepair([p.id]).catch((err) => setError(err instanceof Error ? err.message : String(err))); } }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [filteredPoints, focusIdx, expandedId]);

  const toggleSelect = (id: string) => {
    setSelected((prev) => { const next = new Set(prev); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  };

  const clearFilters = () => { setSearchParams({}, { replace: true }); };

  return (
    <div className="p-6 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold text-gray-900">Source Points</h1>
          {lastRefresh && <Timestamp date={lastRefresh.toISOString()} className="text-xs text-gray-400" />}
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" loading={fetching} icon={<RefreshCw className="w-4 h-4" />} onClick={handleFetchSources} title="Fetch source points from codewiki_lite and run incremental analysis">Fetch Sources</Button>
          <Button loading={repairingAll} icon={<Wrench className="w-4 h-4" />} onClick={() => setConfirmRepairAll(true)}>Repair All</Button>
        </div>
      </div>

      {/* Pipeline state */}
      <ConfirmDialog
        open={confirmRepairAll}
        title="确认修复全部 Source？"
        description={`将启动 ${filteredPoints.length} 个 agent 并发修复，可能耗时较长。`}
        confirmLabel="开始修复"
        onConfirm={() => { setConfirmRepairAll(false); handleRepairAll(); }}
        onCancel={() => setConfirmRepairAll(false)}
      />

      {analyzeState && analyzeState.state !== 'idle' && (
        <div className="flex items-center gap-3 rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-blue-800 animate-fade-in">
          <span className="inline-block w-2 h-2 rounded-full bg-blue-500 animate-pulse" />
          <span className="font-medium capitalize">{analyzeState.state}</span>
          {analyzeState.progress > 0 && analyzeState.progress < 1 && (
            <ProgressBar value={analyzeState.progress} className="flex-1 max-w-xs" />
          )}
          {analyzeState.mode && <Badge tone="blue">{analyzeState.mode}</Badge>}
        </div>
      )}

      {/* Stats + Search */}
      <div className="flex items-center gap-3 flex-wrap">
        <Badge tone="gray">{points.length} total</Badge>
        {Object.entries(statusCounts).map(([s, c]) => (
          <Badge key={s} tone={STATUS_TONE[s] ?? 'gray'}>{s}: {c}</Badge>
        ))}
        {stats && <>
          <Badge tone="purple">LLM Edges: {stats.total_repair_logs ?? 0}</Badge>
          <Badge tone="sky">Feedback: {stats.total_feedback ?? 0}</Badge>
        </>}
        <div className="flex gap-1 border rounded-lg overflow-hidden ml-2">
          <button className={`px-2 py-1 text-xs ${viewMode === 'list' ? 'bg-blue-100 text-blue-700' : 'text-gray-600'}`} onClick={() => setViewMode('list')}>List</button>
          <button className={`px-2 py-1 text-xs ${viewMode === 'module' ? 'bg-blue-100 text-blue-700' : 'text-gray-600'}`} onClick={() => setViewMode('module')}>Module</button>
        </div>
        <SearchInput value={search} onChange={setSearch} placeholder="Search signature, file, module…" className="ml-auto w-64" />
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm flex items-center gap-2 animate-fade-in">
          <XCircle className="w-4 h-4 flex-shrink-0" />
          {error}
        </div>
      )}

      {hasFilter && (
        <div className="flex items-center gap-2 rounded-lg border border-blue-200 bg-blue-50 p-2 text-sm text-blue-800">
          <span>Filtered:</span>
          {filterStatus && <Badge tone="blue">status={filterStatus}</Badge>}
          {filterCaller && <Badge tone="blue">{filterCaller.slice(0, 12)}…</Badge>}
          {filterCategory && <Badge tone="blue">category={filterCategory}</Badge>}
          <button onClick={clearFilters} className="ml-auto text-xs underline opacity-70 hover:opacity-100">Clear</button>
        </div>
      )}

      {/* Keyboard hint + batch hint */}
      <div className="flex items-center justify-between text-xs text-gray-500 bg-gray-50 rounded-lg px-3 py-2 border border-gray-100">
        <div className="flex items-center gap-3">
          <kbd className="px-1.5 py-0.5 rounded bg-white border border-gray-200 text-gray-600 font-mono shadow-sm">j</kbd>/<kbd className="px-1.5 py-0.5 rounded bg-white border border-gray-200 text-gray-600 font-mono shadow-sm">k</kbd> navigate
          <kbd className="px-1.5 py-0.5 rounded bg-white border border-gray-200 text-gray-600 font-mono shadow-sm">Enter</kbd> expand
          <kbd className="px-1.5 py-0.5 rounded bg-white border border-gray-200 text-gray-600 font-mono shadow-sm">Space</kbd> select
          <kbd className="px-1.5 py-0.5 rounded bg-white border border-gray-200 text-gray-600 font-mono shadow-sm">r</kbd> repair
        </div>
        <span className="text-gray-400">Use checkboxes to batch-repair multiple sources</span>
      </div>

      {/* Source list */}
      <div ref={listRef} className="bg-white border border-gray-200 rounded-xl shadow-card divide-y divide-gray-100 overflow-hidden">
        {loading ? (
          <div className="p-4 space-y-3"><SkeletonCard /><SkeletonCard /><SkeletonCard /></div>
        ) : filteredPoints.length === 0 ? (
          <EmptyState
            title={hasFilter ? 'No matches' : 'No source points'}
            description={hasFilter ? 'No source points match the current filter.' : 'Click "Fetch Sources" to scan the codebase and load entry points from codewiki_lite.'}
            action={hasFilter ? { label: 'Clear filters', onClick: clearFilters } : { label: 'Fetch Sources', onClick: handleFetchSources }}
          />
        ) : viewMode === 'module' && groupedByModule ? (
          [...groupedByModule.entries()].map(([mod, items]) => (
            <details key={mod} className="group">
              <summary className="flex items-center gap-2 px-4 py-2 cursor-pointer hover:bg-gray-50 text-sm font-medium text-gray-700">
                <ChevronRight className="w-4 h-4 group-open:rotate-90 transition-transform" />
                {mod} <Badge tone="gray">{items.length}</Badge>
              </summary>
              <div className="divide-y divide-gray-100">
                {items.map((p) => {
                  const prog = progress.get(p.function_id ?? '') || progress.get(p.id);
                  const isExpanded = expandedId === p.id;
                  const isSelected = selected.has(p.id);
                  const fixed = prog?.gaps_fixed ?? 0;
                  const total = prog?.gaps_total ?? 0;
                  return (
                    <div key={p.id}>
                      <div
                        className={`flex items-center gap-3 px-4 py-3 pl-10 cursor-pointer transition-colors duration-150 ${isExpanded ? 'bg-blue-50/50' : 'hover:bg-gray-50'}`}
                        onClick={() => setExpandedId(isExpanded ? null : p.id)}
                        role="button"
                        aria-expanded={isExpanded}
                      >
                        <input type="checkbox" checked={isSelected} onChange={(e) => { e.stopPropagation(); toggleSelect(p.id); }} onClick={(e) => e.stopPropagation()} className="w-4 h-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500" />
                        <div className="flex-1 min-w-0">
                          <div className="font-mono text-sm truncate text-gray-900">{p.signature}</div>
                          <div className="text-xs text-gray-500">{p.file}:{p.line}</div>
                        </div>
                        {total > 0 ? (
                          <div className="flex items-center gap-2 text-xs text-gray-600 shrink-0">
                            <span className="text-green-700 font-medium">{Math.min(fixed, total)} repaired</span>
                            {total - Math.min(fixed, total) > 0 && <span className="text-red-600">{total - Math.min(fixed, total)} unresolved</span>}
                            <span className="text-gray-400">({Math.round(Math.min(fixed, total) / total * 100)}%)</span>
                            <ProgressBar value={Math.min(fixed, total) / total} className="w-16" />
                          </div>
                        ) : (
                          <Badge tone={STATUS_TONE[p.status ?? 'pending'] ?? 'gray'} title={STATUS_TOOLTIP[p.status ?? 'pending']}>{p.status ?? 'pending'}</Badge>
                        )}
                      </div>
                      {isExpanded && <SourceDetail source={p} />}
                    </div>
                  );
                })}
              </div>
            </details>
          ))
        ) : (
          filteredPoints.map((p, idx) => {
            const prog = progress.get(p.function_id ?? '') || progress.get(p.id);
            const isExpanded = expandedId === p.id;
            const isFocused = idx === focusIdx;
            const isSelected = selected.has(p.id);
            const fixed = prog?.gaps_fixed ?? 0;
            const total = prog?.gaps_total ?? 0;
            return (
              <div key={p.id} className={isFocused ? 'ring-2 ring-inset ring-blue-400' : ''}>
                <div
                  className={`flex items-center gap-3 px-4 py-3 cursor-pointer transition-colors duration-150 ${isExpanded ? 'bg-blue-50/50' : 'hover:bg-gray-50'}`}
                  onClick={() => setExpandedId(isExpanded ? null : p.id)}
                  role="button"
                  aria-expanded={isExpanded}
                  aria-label={`${p.signature} — ${isExpanded ? 'collapse' : 'expand'}`}
                >
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={(e) => { e.stopPropagation(); toggleSelect(p.id); }}
                    onClick={(e) => e.stopPropagation()}
                    aria-label={`Select ${p.signature}`}
                    className="w-4 h-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                  />
                  <span className="text-gray-400 text-xs w-4">{isExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}</span>
                  <div className="flex-1 min-w-0">
                    <div className="font-mono text-sm truncate text-gray-900">{p.signature}</div>
                    <div className="text-xs text-gray-500">{p.file}:{p.line}</div>
                  </div>
                  {total > 0 ? (
                    <div className="flex items-center gap-2 text-xs text-gray-600 shrink-0">
                      <span className="text-green-700 font-medium">{Math.min(fixed, total)} repaired</span>
                      {total - Math.min(fixed, total) > 0 && <span className="text-red-600">{total - Math.min(fixed, total)} unresolved</span>}
                      <span className="text-gray-400">({Math.round(Math.min(fixed, total) / total * 100)}%)</span>
                      <ProgressBar value={Math.min(fixed, total) / total} className="w-16" />
                    </div>
                  ) : (
                    <Badge tone={STATUS_TONE[p.status ?? 'pending'] ?? 'gray'} title={STATUS_TOOLTIP[p.status ?? 'pending']}>{p.status ?? 'pending'}</Badge>
                  )}
                  <Button variant="ghost" size="sm" icon={<Wrench className="w-3.5 h-3.5" />}
                    onClick={(e) => { e.stopPropagation(); api.triggerRepair([p.id]).catch((err) => setError(err instanceof Error ? err.message : String(err))); }}>
                    Repair
                  </Button>
                </div>
                {isExpanded && <SourceDetail source={p} />}
              </div>
            );
          })
        )}
      </div>

      {/* Batch action bar */}
      {selected.size > 0 && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 bg-white border border-gray-200 rounded-xl shadow-elevated px-6 py-3 flex items-center gap-4 animate-fade-in z-50">
          <span className="text-sm font-medium text-gray-700">{selected.size} selected</span>
          <Button size="sm" icon={<Wrench className="w-3.5 h-3.5" />} onClick={handleRepairSelected} loading={repairingAll}>Repair Selected</Button>
          <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>Clear</Button>
        </div>
      )}
    </div>
  );
}
