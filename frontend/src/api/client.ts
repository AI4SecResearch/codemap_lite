/**
 * API client for codemap-lite backend.
 *
 * Response shapes match the FastAPI routes under codemap_lite/api/routes/.
 * List endpoints return {total, items} pagination wrappers (architecture.md §8).
 */

const BASE_URL = import.meta.env.VITE_API_URL ?? '';

export interface FileNode {
  id: string;
  file_path: string;
  hash: string;
  primary_language: string;
}

export interface FunctionNode {
  id: string;
  name: string;
  signature: string;
  file_path: string;
  start_line: number;
  end_line: number;
  body_hash?: string;
}

export interface CallsEdgeProps {
  resolved_by: string;
  call_type: string;
  call_file: string;
  call_line: number;
}

export interface CallEdge {
  caller_id: string;
  callee_id: string;
  props: CallsEdgeProps;
}

export interface UnresolvedCall {
  caller_id: string;
  call_expression: string;
  call_file: string;
  call_line: number;
  call_type: string;
  source_code_snippet?: string;
  var_name?: string;
  var_type?: string;
  candidates?: string[];
  retry_count?: number;
  status?: string;
  /**
   * ISO-8601 UTC timestamp of the most recent failed repair attempt
   * (architecture.md §3 Retry 审计字段). Written by the orchestrator
   * each time the gate check fails; undefined means either never
   * attempted or no graph_store was configured.
   */
  last_attempt_timestamp?: string | null;
  /**
   * Human-readable reason (≤200 chars, `<category>: <summary>` or standalone
   * category) for the last failed attempt. `<category>` ∈ `{gate_failed,
   * agent_error, subprocess_timeout, subprocess_crash,
   * agent_exited_without_edge}`. Surfaced in SourcePointList GapDetail so
   * reviewers don't need to read JSONL logs.
   */
  last_attempt_reason?: string | null;
  id?: string;
}

export interface SourcePoint {
  id: string;
  module: string;
  kind: string;
  reason: string;
  signature: string;
  file: string;
  line: number;
  function_id?: string;
  status?: string;
}

export interface Stats {
  total_functions: number;
  total_files: number;
  total_calls: number;
  total_unresolved: number;
  /**
   * Convenience count of llm-repaired CALLS edges (architecture.md §8).
   * Equals `calls_by_resolved_by['llm']` — surfaced so the Dashboard
   * can render the "LLM Repaired" StatCard without drilling into the
   * bucket breakdown (北极星指标 #2 调用链可信度).
   */
  total_llm_edges?: number;
  total_source_points: number;
  source_points_by_status?: Record<string, number>;
  /**
   * Breakdown of `total_unresolved` by `UnresolvedCall.status`
   * (architecture.md §3 GAP 生命周期). Keys are the status string
   * (`"pending"` or `"unresolvable"`); values are counts. Optional
   * for backward compat with older stubs.
   */
  unresolved_by_status?: Record<string, number>;
  /**
   * Breakdown of `total_unresolved` by the `<category>:` prefix of
   * `UnresolvedCall.last_attempt_reason` (architecture.md §3 Retry
   * 审计字段 5 档: `gate_failed` / `agent_error` / `subprocess_crash`
   * / `subprocess_timeout` / `agent_exited_without_edge`). GAPs without
   * an audit stamp (never retried or legacy format) bucket to `"none"`.
   * Drives the Dashboard "Retry reasons" chip row per architecture.md §5
   * drill-down 契约; chip tones mirror GapDetail last-attempt 分色.
   * Optional for backward compat with older stubs.
   */
  unresolved_by_category?: Record<string, number>;
  /**
   * Breakdown of `total_calls` by `CallsEdgeProps.resolved_by`
   * (architecture.md §4). Keys are the 5-valued resolver enum
   * (`symbol_table` / `signature` / `dataflow` / `context` / `llm`);
   * values are counts. Surfaces the llm-repaired edge backlog on
   * the Dashboard so reviewers can see how many CALLS edges are
   * review-critical per architecture.md §5 (审阅对象：
   * 单条 CALLS 边，特别是 resolved_by='llm' 的). Optional for
   * backward compat with older stubs.
   */
  calls_by_resolved_by?: Record<string, number>;
  /**
   * Counter-example library size (architecture.md §3 反馈机制 + §8).
   * Drives the live count chip next to the left-nav "Feedback" label so
   * reviewers notice newly deduplicated patterns without mounting
   * FeedbackLog (北极星指标 #5 状态透明度 + 候选优化方向 #4
   * 进度与可观测性). Optional for backward compat with older stubs.
   */
  total_feedback?: number;
  /**
   * Total `RepairLogNode` count (architecture.md §4 RepairLog schema +
   * ADR #51 属性引用契约). Each successful llm-resolved CALLS edge has
   * a paired RepairLog row recording llm_response + reasoning_summary +
   * timestamp; this field exposes the cumulative repair provenance
   * volume so the Dashboard can advertise "N edges repaired by llm
   * (audit trail available)" without hitting `/repair-logs`. Optional
   * for backward compat with older stubs.
   */
  total_repair_logs?: number;
}

export interface SourceProgress {
  source_id: string;
  gaps_fixed: number;
  gaps_total: number;
  current_gap?: string | null;
  attempt?: number | null;
  max_attempts?: number | null;
  gate_result?: 'pending' | 'passed' | 'failed' | null;
  edges_written?: number | null;
  state?: 'waiting' | 'running' | 'gate_checking' | 'succeeded' | 'failed' | null;
  last_error?: string | null;
}

export interface AnalyzeStatus {
  state: string;
  progress: number;
  mode?: string;
  sources?: SourceProgress[];
  started_at?: string | null;
  completed_at?: string | null;
  error?: string | null;
}

export interface Subgraph {
  nodes: FunctionNode[];
  edges: CallEdge[];
  unresolved: UnresolvedCall[];
}

export interface Review {
  id: string;
  caller_id: string;
  callee_id: string;
  call_file: string;
  call_line: number;
  verdict: 'correct' | 'incorrect';
  comment?: string | null;
}

/**
 * Generalized counter example persisted by the backend FeedbackStore
 * (architecture.md §3 反馈机制). Returned by `GET /api/v1/feedback` and
 * injected into the next repair round's CLAUDE.md.
 */
export interface CounterExample {
  call_context: string;
  wrong_target: string;
  correct_target: string;
  pattern: string;
}

/**
 * Repair provenance record persisted alongside an llm-resolved CALLS
 * edge (architecture.md §4 RepairLog schema + ADR #51 属性引用契约).
 *
 * The `(caller_id, callee_id, call_location)` triple locates the
 * matching CALLS edge — there is no relationship edge between RepairLog
 * and Function nodes (per ADR #51, attribute-only association). The
 * frontend uses this to populate an inspector panel when reviewers
 * click an llm-repaired edge in CallGraphView.
 */
export interface RepairLog {
  id: string;
  caller_id: string;
  callee_id: string;
  /** Format: `<file>:<line>` (e.g. `foo.cpp:42`). */
  call_location: string;
  /** Always `"llm"` today; reserved for future repair methods. */
  repair_method: string;
  /** Raw agent stdout/llm reply that produced the resolution. */
  llm_response: string;
  /** ISO-8601 UTC timestamp of when the edge was written. */
  timestamp: string;
  /** Human-readable summary of the agent's reasoning chain. */
  reasoning_summary: string;
  /** Source point ID that triggered this repair session. */
  source_id?: string;
}

/**
 * Response shape for `POST /api/v1/feedback`. Echoes the persisted
 * example plus two signal fields:
 *
 * - `deduplicated`: `true` when the submitted pattern matched an
 *   existing entry and was merged (architecture.md §3 反馈机制 step 4).
 * - `total`: current library size after the operation.
 *
 * These let the UI tell the reviewer whether their submission broadened
 * an existing rule or opened a new one without an extra round trip
 * (北极星指标 #5 状态透明度 — 反例命中).
 */
export interface CounterExampleCreateResult extends CounterExample {
  deduplicated: boolean;
  total: number;
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${path}`, init);
  } catch {
    throw new Error(`Cannot reach backend (${BASE_URL}${path}) — is the server running?`);
  }
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`API ${res.status} ${res.statusText}: ${body.slice(0, 200)}`);
  }
  return res.json();
}

export const api = {
  // Graph browsing
  getFiles: (params?: { limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (params?.limit != null) qs.set('limit', String(params.limit));
    if (params?.offset != null) qs.set('offset', String(params.offset));
    const q = qs.toString();
    return fetchJson<{ total: number; items: FileNode[] }>(
      `/api/v1/files${q ? `?${q}` : ''}`
    );
  },
  getFunctions: (params?: { file?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (params?.file) qs.set('file', params.file);
    if (params?.limit != null) qs.set('limit', String(params.limit));
    if (params?.offset != null) qs.set('offset', String(params.offset));
    const q = qs.toString();
    return fetchJson<{ total: number; items: FunctionNode[] }>(
      `/api/v1/functions${q ? `?${q}` : ''}`
    );
  },
  getFunction: (id: string) =>
    fetchJson<FunctionNode>(`/api/v1/functions/${id}`),
  getCallers: (id: string, params?: { limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (params?.limit != null) qs.set('limit', String(params.limit));
    if (params?.offset != null) qs.set('offset', String(params.offset));
    const q = qs.toString();
    return fetchJson<{ total: number; items: FunctionNode[] }>(
      `/api/v1/functions/${id}/callers${q ? `?${q}` : ''}`
    );
  },
  getCallees: (id: string, params?: { limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (params?.limit != null) qs.set('limit', String(params.limit));
    if (params?.offset != null) qs.set('offset', String(params.offset));
    const q = qs.toString();
    return fetchJson<{ total: number; items: FunctionNode[] }>(
      `/api/v1/functions/${id}/callees${q ? `?${q}` : ''}`
    );
  },
  getCallChain: (id: string, depth = 5) =>
    fetchJson<Subgraph>(`/api/v1/functions/${id}/call-chain?depth=${depth}`),
  getSourceCode: (file: string, start: number, end: number) =>
    fetchJson<{ file: string; start_line: number; end_line: number; content: string }>(
      `/api/v1/source-code?file=${encodeURIComponent(file)}&start=${start}&end=${end}`
    ),
  listUnresolved: (params?: {
    limit?: number;
    offset?: number;
    caller?: string;
    status?: string;
    category?: string;
  }) => {
    const qs = new URLSearchParams();
    const limit = params?.limit ?? 200;
    const offset = params?.offset ?? 0;
    qs.set('limit', String(limit));
    qs.set('offset', String(offset));
    if (params?.caller) qs.set('caller', params.caller);
    if (params?.status) qs.set('status', params.status);
    if (params?.category) qs.set('category', params.category);
    return fetchJson<{ total: number; items: UnresolvedCall[] }>(
      `/api/v1/unresolved-calls?${qs.toString()}`
    );
  },

  // Source points
  getSourcePoints: (params?: { kind?: string; module?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (params?.kind) qs.set('kind', params.kind);
    if (params?.module) qs.set('module', params.module);
    if (params?.limit != null) qs.set('limit', String(params.limit));
    if (params?.offset != null) qs.set('offset', String(params.offset));
    const q = qs.toString();
    return fetchJson<{ total: number; items: SourcePoint[] }>(
      `/api/v1/source-points${q ? `?${q}` : ''}`
    );
  },
  getSourcePointSummary: () =>
    fetchJson<{ total: number; by_kind: Record<string, number>; by_status: Record<string, number> }>(
      '/api/v1/source-points/summary'
    ),
  getReachable: (id: string) =>
    fetchJson<Subgraph>(`/api/v1/source-points/${id}/reachable`),

  // Stats & analyze
  getStats: () => fetchJson<Stats>('/api/v1/stats'),
  getAnalyzeStatus: () => fetchJson<AnalyzeStatus>('/api/v1/analyze/status'),
  triggerAnalyze: (mode: 'full' | 'incremental') =>
    fetchJson<{ status: string; mode: string }>('/api/v1/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    }),
  triggerRepair: (sourceIds?: string[]) =>
    fetchJson<{ status: string; action: string }>('/api/v1/analyze/repair', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: sourceIds?.length ? JSON.stringify({ source_ids: sourceIds }) : undefined,
    }),

  // Reviews & manual edges
  getReviews: () => fetchJson<{ total: number; items: Review[] }>('/api/v1/reviews'),
  createReview: (data: {
    caller_id: string;
    callee_id: string;
    call_file: string;
    call_line: number;
    verdict: 'correct' | 'incorrect';
    comment?: string;
    correct_target?: string;
  }) =>
    fetchJson<Review>('/api/v1/reviews', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),
  updateReview: (id: string, data: { comment?: string; status?: string }) =>
    fetchJson<Review>(`/api/v1/reviews/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),
  deleteReview: (id: string) =>
    fetch(`${BASE_URL}/api/v1/reviews/${id}`, { method: 'DELETE' }),
  createEdge: (edge: {
    caller_id: string;
    callee_id: string;
    resolved_by: string;
    call_type: string;
    call_file: string;
    call_line: number;
  }) =>
    fetchJson<{ caller_id: string; callee_id: string; status: string }>(
      '/api/v1/edges',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(edge),
      }
    ),
  /** Delete a specific CALLS edge with cascade (architecture.md §5). */
  deleteEdge: (edge: {
    caller_id: string;
    callee_id: string;
    call_file: string;
    call_line: number;
    correct_target?: string;
  }) =>
    fetch(`${BASE_URL}/api/v1/edges`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(edge),
    }),
  /** Bulk-delete all edges touching a function (incremental invalidation). */
  deleteEdgesForFunction: (functionId: string) =>
    fetch(`${BASE_URL}/api/v1/edges/${encodeURIComponent(functionId)}`, {
      method: 'DELETE',
    }),

  // Feedback
  getFeedback: (params?: { limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (params?.limit != null) qs.set('limit', String(params.limit));
    if (params?.offset != null) qs.set('offset', String(params.offset));
    const q = qs.toString();
    return fetchJson<{ total: number; items: CounterExample[] }>(
      `/api/v1/feedback${q ? `?${q}` : ''}`
    );
  },
  createFeedback: (example: CounterExample) =>
    fetchJson<CounterExampleCreateResult>('/api/v1/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(example),
    }),

  // Live agent log tail (ADR-0008)
  getLiveLog: (sourceId: string, tail?: number) =>
    fetchJson<{ lines: string[]; attempt: number; finished: boolean; source_id: string }>(
      `/api/v1/repair-logs/live?source_id=${encodeURIComponent(sourceId)}&tail=${tail ?? 30}`
    ),

  // Feedback CRUD (ADR-0008)
  deleteFeedback: (id: number) =>
    fetchJson<{ deleted: boolean; total: number }>(`/api/v1/feedback/${id}`, { method: 'DELETE' }),
  updateFeedback: (id: number, data: Partial<CounterExample>) =>
    fetchJson<CounterExample>(`/api/v1/feedback/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),

  // Repair logs (architecture.md §4 + §8 + ADR #51)
  getRepairLogs: (params?: {
    caller?: string;
    callee?: string;
    location?: string;
    source?: string;
    source_reachable?: string;
    limit?: number;
    offset?: number;
  }) => {
    const qs = new URLSearchParams();
    if (params?.caller) qs.set('caller', params.caller);
    if (params?.callee) qs.set('callee', params.callee);
    if (params?.location) qs.set('location', params.location);
    if (params?.source) qs.set('source', params.source);
    if (params?.source_reachable) qs.set('source_reachable', params.source_reachable);
    if (params?.limit != null) qs.set('limit', String(params.limit));
    if (params?.offset != null) qs.set('offset', String(params.offset));
    const q = qs.toString();
    return fetchJson<{ total: number; items: RepairLog[] }>(
      `/api/v1/repair-logs${q ? `?${q}` : ''}`
    );
  },
};
