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
}

export interface AnalyzeStatus {
  state: string;
  progress: number;
  mode?: string;
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
  getFeedback: () => fetchJson<unknown[]>('/api/v1/feedback'),
};
