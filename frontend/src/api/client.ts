/**
 * API client for codemap-lite backend.
 *
 * Response shapes match the FastAPI routes under codemap_lite/api/routes/.
 * Most list endpoints return arrays directly (not wrapped objects).
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
   * Human-readable reason (≤200 chars, `<category>: <summary>`) for the
   * last failed attempt. `<category>` ∈ `{gate_failed, agent_error,
   * subprocess_timeout, subprocess_crash}`. Surfaced in ReviewQueue
   * GapDetail so reviewers don't need to read JSONL logs.
   */
  last_attempt_reason?: string | null;
  id?: string;
}

export interface SourcePoint {
  id: string;
  module: string;
  layer: number;
  kind: string;
  reason: string;
  signature: string;
  file: string;
  line: number;
  reachable_sinks?: unknown[];
  taint_paths?: unknown[];
}

export interface Stats {
  total_functions: number;
  total_files: number;
  total_calls: number;
  total_unresolved: number;
  total_source_points: number;
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
   * 审计字段 4 档: `gate_failed` / `agent_error` / `subprocess_crash`
   * / `subprocess_timeout`). GAPs without an audit stamp (never retried
   * or legacy format) bucket to `"none"`. Drives the Dashboard
   * "Retry reasons" chip row per architecture.md §5 drill-down 契约;
   * chip tones mirror GapDetail last-attempt 分色. Optional for
   * backward compat with older stubs.
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
}

export interface SourceProgress {
  source_id: string;
  gaps_fixed: number;
  gaps_total: number;
  current_gap?: string | null;
}

export interface AnalyzeStatus {
  state: string;
  progress: number;
  mode?: string;
  sources?: SourceProgress[];
}

export interface Subgraph {
  nodes: FunctionNode[];
  edges: CallEdge[];
  unresolved: UnresolvedCall[];
}

export interface Review {
  id: string;
  function_id: string;
  comment: string;
  status: string;
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
  const res = await fetch(`${BASE_URL}${path}`, init);
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`API ${res.status} ${res.statusText}: ${body.slice(0, 200)}`);
  }
  return res.json();
}

export const api = {
  // Graph browsing
  getFiles: () => fetchJson<FileNode[]>('/api/v1/files'),
  getFunctions: (file?: string) =>
    fetchJson<FunctionNode[]>(
      `/api/v1/functions${file ? `?file=${encodeURIComponent(file)}` : ''}`
    ),
  getFunction: (id: string) =>
    fetchJson<FunctionNode>(`/api/v1/functions/${id}`),
  getCallers: (id: string) =>
    fetchJson<FunctionNode[]>(`/api/v1/functions/${id}/callers`),
  getCallees: (id: string) =>
    fetchJson<FunctionNode[]>(`/api/v1/functions/${id}/callees`),
  getCallChain: (id: string, depth = 5) =>
    fetchJson<Subgraph>(`/api/v1/functions/${id}/call-chain?depth=${depth}`),
  listUnresolved: (limit = 200, offset = 0) =>
    fetchJson<{ total: number; items: UnresolvedCall[] }>(
      `/api/v1/unresolved-calls?limit=${limit}&offset=${offset}`
    ),

  // Source points
  getSourcePoints: (params?: { kind?: string; module?: string }) => {
    const qs = new URLSearchParams();
    if (params?.kind) qs.set('kind', params.kind);
    if (params?.module) qs.set('module', params.module);
    const q = qs.toString();
    return fetchJson<SourcePoint[]>(
      `/api/v1/source-points${q ? `?${q}` : ''}`
    );
  },
  getSourcePointSummary: () =>
    fetchJson<{ total: number; by_kind: Record<string, number> }>(
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
  triggerRepair: () =>
    fetchJson<{ status: string; action: string }>('/api/v1/analyze/repair', {
      method: 'POST',
    }),

  // Reviews & manual edges
  getReviews: () => fetchJson<Review[]>('/api/v1/reviews'),
  createReview: (data: { function_id: string; comment: string; status: string }) =>
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
  deleteEdges: (functionId: string) =>
    fetch(`${BASE_URL}/api/v1/edges/${encodeURIComponent(functionId)}`, {
      method: 'DELETE',
    }),

  // Feedback
  getFeedback: () => fetchJson<CounterExample[]>('/api/v1/feedback'),
  createFeedback: (example: CounterExample) =>
    fetchJson<CounterExampleCreateResult>('/api/v1/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(example),
    }),
};
