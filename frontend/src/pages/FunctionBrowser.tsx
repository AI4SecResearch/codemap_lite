import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, FunctionNode } from '../api/client';

function groupByFile(functions: FunctionNode[]): Map<string, FunctionNode[]> {
  const m = new Map<string, FunctionNode[]>();
  for (const f of functions) {
    const list = m.get(f.file_path);
    if (list) list.push(f);
    else m.set(f.file_path, [f]);
  }
  return m;
}

// architecture.md §5 跨页面 drill-down 契约：每行函数挂一个 GAP count
// chip，点击跳 /review?caller=<id>。Chip 颜色按 backlog 大小分三档
// (gray 0 / amber 1-2 / red 3+) 与其它 backlog surface（nav chip、
// Dashboard StatCard）保持同一视觉语言（北极星 #1 + #2 + #5）。
function GapChip({ count, href }: { count: number; href: string }) {
  if (count === 0) return null;
  const tone =
    count >= 3
      ? 'bg-red-100 text-red-800 hover:bg-red-200'
      : 'bg-amber-100 text-amber-800 hover:bg-amber-200';
  return (
    <Link
      to={href}
      className={`inline-flex items-center justify-center min-w-[1.25rem] px-1.5 rounded-full text-[10px] font-semibold leading-[1.125rem] ${tone}`}
      title={`${count} unresolved GAP${count === 1 ? '' : 's'} — review`}
    >
      {count}
    </Link>
  );
}

export default function FunctionBrowser() {
  const [functions, setFunctions] = useState<FunctionNode[]>([]);
  const [gapCounts, setGapCounts] = useState<Map<string, number>>(new Map());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [fileSearch, setFileSearch] = useState('');
  const [fnSearch, setFnSearch] = useState('');

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        // api.listUnresolved already supports pagination; 500 covers the
        // CastEngine baseline with headroom. A missing/failed call just
        // leaves the chip map empty (non-blocking — GAP chip is a
        // surface affordance, not the page's primary payload).
        const [fns, unresolved] = await Promise.all([
          api.getFunctions(),
          api.listUnresolved({ limit: 500 }).catch(() => ({ total: 0, items: [] })),
        ]);
        if (cancelled) return;
        setFunctions(fns);
        const counts = new Map<string, number>();
        for (const g of unresolved.items) {
          counts.set(g.caller_id, (counts.get(g.caller_id) ?? 0) + 1);
        }
        setGapCounts(counts);
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

  const grouped = useMemo(() => groupByFile(functions), [functions]);
  const files = useMemo(() => Array.from(grouped.keys()).sort(), [grouped]);

  // Per-file GAP sums so the left file tree can surface which files
  // host the heaviest backlog without the reviewer opening each one.
  const fileGapSums = useMemo(() => {
    const sums = new Map<string, number>();
    for (const [file, fns] of grouped.entries()) {
      let total = 0;
      for (const f of fns) total += gapCounts.get(f.id) ?? 0;
      sums.set(file, total);
    }
    return sums;
  }, [grouped, gapCounts]);

  const filteredFiles = useMemo(() => {
    const term = fileSearch.trim().toLowerCase();
    if (!term) return files;
    return files.filter((f) => f.toLowerCase().includes(term));
  }, [files, fileSearch]);

  const shownFile = selectedFile ?? filteredFiles[0] ?? null;
  const fileFunctions = shownFile ? grouped.get(shownFile) ?? [] : [];

  const filteredFunctions = useMemo(() => {
    const term = fnSearch.trim().toLowerCase();
    if (!term) return fileFunctions;
    return fileFunctions.filter(
      (f) =>
        f.name.toLowerCase().includes(term) ||
        f.signature.toLowerCase().includes(term)
    );
  }, [fileFunctions, fnSearch]);

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Function Browser</h1>
        <div className="text-sm text-gray-500">
          {files.length} files · {functions.length} functions
        </div>
      </div>

      {error ? (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded p-3 text-sm">
          {error}
        </div>
      ) : null}

      <div className="flex gap-4 h-[calc(100vh-200px)]">
        <div className="w-1/3 border rounded bg-white flex flex-col">
          <div className="p-3 border-b">
            <input
              className="w-full border rounded px-2 py-1 text-sm"
              placeholder="Filter files…"
              value={fileSearch}
              onChange={(e) => setFileSearch(e.target.value)}
            />
          </div>
          <div className="flex-1 overflow-auto">
            {loading ? (
              <div className="p-4 text-gray-500 text-sm">Loading…</div>
            ) : filteredFiles.length === 0 ? (
              <div className="p-4 text-gray-500 text-sm">No files match.</div>
            ) : (
              <ul className="text-xs font-mono">
                {filteredFiles.map((f) => {
                  const isSel = f === shownFile;
                  const count = grouped.get(f)?.length ?? 0;
                  const gaps = fileGapSums.get(f) ?? 0;
                  const short = f.split('/').slice(-3).join('/');
                  return (
                    <li
                      key={f}
                      className={`px-3 py-1 cursor-pointer border-b hover:bg-gray-50 ${
                        isSel ? 'bg-blue-50 text-blue-700' : ''
                      }`}
                      title={f}
                      onClick={() => setSelectedFile(f)}
                    >
                      <div className="flex items-center gap-1.5">
                        <span className="truncate flex-1">{short}</span>
                        {gaps > 0 ? (
                          <span
                            className={`shrink-0 inline-flex items-center justify-center min-w-[1rem] px-1 rounded-full text-[9px] font-semibold leading-[1rem] ${
                              gaps >= 3
                                ? 'bg-red-100 text-red-700'
                                : 'bg-amber-100 text-amber-700'
                            }`}
                            title={`${gaps} unresolved GAP${gaps === 1 ? '' : 's'} in this file`}
                          >
                            {gaps}
                          </span>
                        ) : null}
                      </div>
                      <div className="text-[10px] text-gray-500">
                        {count} fn
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>

        <div className="flex-1 border rounded bg-white flex flex-col">
          <div className="p-3 border-b flex items-center gap-2">
            <input
              className="flex-1 border rounded px-2 py-1 text-sm"
              placeholder="Filter functions…"
              value={fnSearch}
              onChange={(e) => setFnSearch(e.target.value)}
            />
            <span className="text-xs text-gray-500">
              {filteredFunctions.length} / {fileFunctions.length}
            </span>
          </div>
          <div className="flex-1 overflow-auto">
            {!shownFile ? (
              <div className="p-4 text-gray-500 text-sm">
                Select a file from the left panel.
              </div>
            ) : filteredFunctions.length === 0 ? (
              <div className="p-4 text-gray-500 text-sm">
                No functions match.
              </div>
            ) : (
              <table className="w-full text-sm">
                <thead className="bg-gray-50 sticky top-0">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium text-gray-500">
                      Name
                    </th>
                    <th className="px-3 py-2 text-left font-medium text-gray-500">
                      Signature
                    </th>
                    <th className="px-3 py-2 text-left font-medium text-gray-500">
                      Lines
                    </th>
                    <th className="px-3 py-2 text-left font-medium text-gray-500">
                      GAPs
                    </th>
                    <th className="px-3 py-2 text-left font-medium text-gray-500">
                      Action
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {filteredFunctions.map((f) => {
                    const gapCount = gapCounts.get(f.id) ?? 0;
                    return (
                      <tr key={f.id} className="hover:bg-gray-50 align-top">
                        <td className="px-3 py-2 font-mono text-xs">{f.name}</td>
                        <td className="px-3 py-2 font-mono text-xs text-gray-600">
                          {f.signature}
                        </td>
                        <td className="px-3 py-2 text-xs text-gray-500">
                          {f.start_line}-{f.end_line}
                        </td>
                        <td className="px-3 py-2">
                          <GapChip
                            count={gapCount}
                            href={`/review?caller=${encodeURIComponent(f.id)}`}
                          />
                          {gapCount === 0 ? (
                            <span className="text-xs text-gray-300">—</span>
                          ) : null}
                        </td>
                        <td className="px-3 py-2">
                          <Link
                            to={`/graph?function=${encodeURIComponent(f.id)}`}
                            className="text-blue-600 hover:underline text-xs"
                          >
                            View chain →
                          </Link>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
