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

export default function FunctionBrowser() {
  const [functions, setFunctions] = useState<FunctionNode[]>([]);
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
        const data = await api.getFunctions();
        if (!cancelled) setFunctions(data);
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
                      <div className="truncate">{short}</div>
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
                      Action
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {filteredFunctions.map((f) => (
                    <tr key={f.id} className="hover:bg-gray-50 align-top">
                      <td className="px-3 py-2 font-mono text-xs">{f.name}</td>
                      <td className="px-3 py-2 font-mono text-xs text-gray-600">
                        {f.signature}
                      </td>
                      <td className="px-3 py-2 text-xs text-gray-500">
                        {f.start_line}-{f.end_line}
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
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
