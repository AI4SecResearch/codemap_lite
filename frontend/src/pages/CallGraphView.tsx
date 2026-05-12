import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import cytoscape, { Core, ElementDefinition, NodeSingular } from 'cytoscape';
import { api, Subgraph, FunctionNode, UnresolvedCall } from '../api/client';

type StartKind = 'function' | 'source' | null;

function shortFile(p: string): string {
  return p.split('/').slice(-3).join('/');
}

function moduleOf(filePath: string): string {
  // Use the first path segment after any "/mnt/c/Task/openHarmony/foundation/CastEngine/"
  // prefix. Falls back to the first segment.
  const parts = filePath.replace(/\\/g, '/').split('/').filter(Boolean);
  // Prefer a segment that looks like a castengine module
  const castIdx = parts.findIndex((p) => p.startsWith('castengine_'));
  if (castIdx >= 0) return parts[castIdx];
  return parts[0] ?? 'unknown';
}

function buildElements(
  graph: Subgraph,
  rootId: string | null
): ElementDefinition[] {
  const elements: ElementDefinition[] = [];
  const moduleSet = new Set<string>();
  const fileSet = new Set<string>();

  for (const n of graph.nodes) {
    const mod = moduleOf(n.file_path);
    moduleSet.add(mod);
    fileSet.add(n.file_path);
  }

  // Compound: module nodes
  for (const m of moduleSet) {
    elements.push({
      data: { id: `mod::${m}`, label: m, kind: 'module' },
      classes: 'module',
    });
  }
  // Compound: file nodes (parent = module)
  for (const f of fileSet) {
    elements.push({
      data: {
        id: `file::${f}`,
        label: shortFile(f),
        kind: 'file',
        parent: `mod::${moduleOf(f)}`,
      },
      classes: 'file',
    });
  }
  // Function nodes (parent = file)
  for (const n of graph.nodes) {
    elements.push({
      data: {
        id: n.id,
        label: n.name,
        kind: 'function',
        parent: `file::${n.file_path}`,
        isRoot: n.id === rootId,
        fn: n,
      },
      classes: n.id === rootId ? 'function root' : 'function',
    });
  }
  // Resolved edges
  for (const e of graph.edges) {
    elements.push({
      data: {
        id: `e::${e.caller_id}->${e.callee_id}::${e.props.call_line}`,
        source: e.caller_id,
        target: e.callee_id,
        kind: 'resolved',
        callType: e.props.call_type,
        resolvedBy: e.props.resolved_by,
      },
      classes: `resolved ${e.props.resolved_by}`,
    });
  }
  // Unresolved calls: synthetic target node + edge
  graph.unresolved.forEach((u, i) => {
    const synthId = `gap::${u.caller_id}::${u.call_line}::${i}`;
    elements.push({
      data: {
        id: synthId,
        label: u.var_name || u.call_expression.slice(0, 40),
        kind: 'unresolved',
        gap: u,
      },
      classes: 'unresolved',
    });
    elements.push({
      data: {
        id: `ue::${synthId}`,
        source: u.caller_id,
        target: synthId,
        kind: 'unresolved',
        callType: u.call_type,
      },
      classes: 'unresolved',
    });
  });

  return elements;
}

function NodeInspector({
  selected,
  onExpand,
}: {
  selected: { kind: string; data: unknown } | null;
  onExpand: (fnId: string) => void;
}) {
  if (!selected) {
    return <p className="text-sm text-gray-500">Click a node to inspect.</p>;
  }
  if (selected.kind === 'function') {
    const fn = selected.data as FunctionNode;
    return (
      <div className="space-y-2 text-xs">
        <div>
          <div className="font-semibold text-sm break-all">{fn.name}</div>
          <div className="text-gray-500 font-mono break-all">{fn.id}</div>
        </div>
        <div>
          <div className="text-gray-500">Signature</div>
          <div className="font-mono break-all">{fn.signature}</div>
        </div>
        <div>
          <div className="text-gray-500">Location</div>
          <div className="font-mono break-all">
            {fn.file_path}:{fn.start_line}-{fn.end_line}
          </div>
        </div>
        <button
          className="mt-2 w-full px-2 py-1 rounded bg-blue-600 text-white text-xs hover:bg-blue-700"
          onClick={() => onExpand(fn.id)}
        >
          Center here
        </button>
      </div>
    );
  }
  if (selected.kind === 'unresolved') {
    const gap = selected.data as UnresolvedCall;
    return (
      <div className="space-y-2 text-xs">
        <div>
          <div className="font-semibold text-sm text-amber-700">
            Unresolved GAP
          </div>
          <div className="text-gray-500">{gap.call_type}</div>
        </div>
        <div>
          <div className="text-gray-500">Expression</div>
          <div className="font-mono break-all">{gap.call_expression}</div>
        </div>
        <div>
          <div className="text-gray-500">Caller</div>
          <div className="font-mono break-all">{gap.caller_id}</div>
        </div>
        <div>
          <div className="text-gray-500">Location</div>
          <div className="font-mono break-all">
            {gap.call_file}:{gap.call_line}
          </div>
        </div>
        {gap.candidates && gap.candidates.length > 0 ? (
          <div>
            <div className="text-gray-500">
              Candidates ({gap.candidates.length})
            </div>
            <ul className="list-disc pl-4">
              {gap.candidates.slice(0, 8).map((c) => (
                <li key={c} className="font-mono break-all">
                  {c}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    );
  }
  if (selected.kind === 'file' || selected.kind === 'module') {
    const data = selected.data as { label: string; id: string };
    return (
      <div className="text-xs space-y-1">
        <div className="font-semibold text-sm">
          {selected.kind === 'module' ? 'Module' : 'File'}: {data.label}
        </div>
        <div className="font-mono break-all text-gray-500">{data.id}</div>
      </div>
    );
  }
  return null;
}

export default function CallGraphView() {
  const [params, setParams] = useSearchParams();
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const [graph, setGraph] = useState<Subgraph | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [depth, setDepth] = useState(5);
  const [selected, setSelected] = useState<{ kind: string; data: unknown } | null>(
    null
  );

  const startKind: StartKind = params.get('function')
    ? 'function'
    : params.get('source')
    ? 'source'
    : null;
  const startId = params.get('function') ?? params.get('source');

  const load = useCallback(async () => {
    if (!startId) {
      setGraph(null);
      return;
    }
    setLoading(true);
    try {
      const data =
        startKind === 'function'
          ? await api.getCallChain(startId, depth)
          : await api.getReachable(startId);
      setGraph(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setGraph(null);
    } finally {
      setLoading(false);
    }
  }, [startId, startKind, depth]);

  useEffect(() => {
    load();
  }, [load]);

  const elements = useMemo(
    () => (graph ? buildElements(graph, startKind === 'function' ? startId : null) : []),
    [graph, startId, startKind]
  );

  useEffect(() => {
    if (!containerRef.current) return;
    if (cyRef.current) {
      cyRef.current.destroy();
      cyRef.current = null;
    }
    if (elements.length === 0) return;

    const cy = cytoscape({
      container: containerRef.current,
      elements,
      style: ([
        {
          selector: 'node.module',
          style: {
            'background-color': '#f1f5f9',
            'border-color': '#94a3b8',
            'border-width': 1,
            label: 'data(label)',
            'font-size': 10,
            color: '#475569',
            'text-valign': 'top',
            'text-halign': 'center',
            padding: 12,
            'background-opacity': 0.5,
          },
        },
        {
          selector: 'node.file',
          style: {
            'background-color': '#e0f2fe',
            'border-color': '#0284c7',
            'border-width': 1,
            label: 'data(label)',
            'font-size': 9,
            color: '#075985',
            'text-valign': 'top',
            'text-halign': 'center',
            padding: 6,
            'background-opacity': 0.4,
          },
        },
        {
          selector: 'node.function',
          style: {
            'background-color': '#60a5fa',
            label: 'data(label)',
            'font-size': 9,
            color: '#0f172a',
            'text-valign': 'center',
            'text-halign': 'center',
            shape: 'round-rectangle',
            width: 'label',
            height: 22,
            padding: 6,
          },
        },
        {
          selector: 'node.root',
          style: {
            'background-color': '#22c55e',
            'border-color': '#15803d',
            'border-width': 2,
          },
        },
        {
          selector: 'node.unresolved',
          style: {
            'background-color': '#fde68a',
            'border-color': '#b45309',
            'border-width': 1,
            'border-style': 'dashed',
            label: 'data(label)',
            'font-size': 8,
            shape: 'diamond',
            width: 'label',
            height: 20,
          },
        },
        {
          selector: 'edge.resolved',
          style: {
            width: 1.5,
            'line-color': '#64748b',
            'target-arrow-color': '#64748b',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
          },
        },
        {
          selector: 'edge.unresolved',
          style: {
            width: 1.5,
            'line-color': '#b45309',
            'line-style': 'dashed',
            'target-arrow-color': '#b45309',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
          },
        },
        {
          selector: ':selected',
          style: {
            'border-color': '#dc2626',
            'border-width': 3,
          },
        },
      ] as unknown) as cytoscape.StylesheetCSS[],
      layout: {
        name: 'breadthfirst',
        directed: true,
        padding: 20,
        spacingFactor: 1.2,
      } as any,
      wheelSensitivity: 0.2,
    });

    cy.on('tap', 'node', (evt) => {
      const n = evt.target as NodeSingular;
      const kind = n.data('kind');
      if (kind === 'function') {
        setSelected({ kind: 'function', data: n.data('fn') });
      } else if (kind === 'unresolved') {
        setSelected({ kind: 'unresolved', data: n.data('gap') });
      } else if (kind === 'file' || kind === 'module') {
        setSelected({ kind, data: { id: n.id(), label: n.data('label') } });
      }
    });
    cy.on('tap', (evt) => {
      if (evt.target === cy) setSelected(null);
    });

    cyRef.current = cy;

    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, [elements]);

  const onExpand = (fnId: string) => {
    setParams({ function: fnId });
  };

  const nodeCount = graph?.nodes.length ?? 0;
  const edgeCount = graph?.edges.length ?? 0;
  const gapCount = graph?.unresolved.length ?? 0;

  return (
    <div className="p-6 h-[calc(100vh-56px)] flex flex-col space-y-3">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Call Graph</h1>
        <div className="flex items-center gap-2 text-sm">
          {startKind === 'function' ? (
            <>
              <label className="text-gray-600">Depth</label>
              <input
                type="number"
                min={1}
                max={20}
                value={depth}
                onChange={(e) =>
                  setDepth(Math.max(1, Math.min(20, Number(e.target.value) || 1)))
                }
                className="border rounded px-2 py-0.5 w-16"
              />
            </>
          ) : null}
          <button
            className="px-3 py-1 rounded border text-sm hover:bg-gray-50"
            onClick={load}
            disabled={loading || !startId}
          >
            Reload
          </button>
        </div>
      </div>

      {!startId ? (
        <div className="bg-white border rounded p-4 text-sm text-gray-600">
          Pick a starting point from{' '}
          <a href="/sources" className="text-blue-600 hover:underline">
            Source Points
          </a>{' '}
          or{' '}
          <a href="/functions" className="text-blue-600 hover:underline">
            Function Browser
          </a>
          .
        </div>
      ) : (
        <div className="bg-white border rounded p-3 text-xs text-gray-600 flex flex-wrap gap-x-4 gap-y-1">
          <span>
            Start: <span className="font-mono">{startKind}</span>{' '}
            <span className="font-mono break-all">{startId}</span>
          </span>
          <span>Functions: {nodeCount}</span>
          <span>Resolved edges: {edgeCount}</span>
          <span className="text-amber-700">GAPs: {gapCount}</span>
        </div>
      )}

      {error ? (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded p-3 text-sm">
          {error}
        </div>
      ) : null}

      <div className="flex-1 flex gap-3 min-h-0">
        <div className="flex-1 border rounded bg-gray-50 relative min-h-[400px]">
          {loading ? (
            <div className="absolute inset-0 flex items-center justify-center text-gray-500 text-sm z-10">
              Loading subgraph…
            </div>
          ) : null}
          {!loading && (!graph || elements.length === 0) ? (
            <div className="absolute inset-0 flex items-center justify-center text-gray-500 text-sm">
              {startId ? 'No reachable graph for this start point.' : ''}
            </div>
          ) : null}
          <div
            ref={containerRef}
            className="absolute inset-0"
            aria-label="Call graph visualization"
          />
        </div>
        <div className="w-80 border rounded p-3 bg-white overflow-auto">
          <h2 className="font-semibold mb-2 text-sm">Node Inspector</h2>
          <NodeInspector selected={selected} onExpand={onExpand} />
        </div>
      </div>
    </div>
  );
}
