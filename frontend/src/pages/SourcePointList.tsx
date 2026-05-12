import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, SourcePoint } from '../api/client';

export default function SourcePointList() {
  const [points, setPoints] = useState<SourcePoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState('');
  const [search, setSearch] = useState('');

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const data = await api.getSourcePoints();
        if (!cancelled) setPoints(data);
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

  const kinds = useMemo(() => {
    const s = new Set<string>();
    points.forEach((p) => s.add(p.kind));
    return Array.from(s).sort();
  }, [points]);

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    return points.filter((p) => {
      if (kindFilter && p.kind !== kindFilter) return false;
      if (!term) return true;
      return (
        p.signature.toLowerCase().includes(term) ||
        p.file.toLowerCase().includes(term) ||
        p.module.toLowerCase().includes(term) ||
        p.reason.toLowerCase().includes(term)
      );
    });
  }, [points, kindFilter, search]);

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Source Points</h1>
        <div className="text-sm text-gray-500">
          {filtered.length} / {points.length} shown
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <input
          className="border rounded px-3 py-1 text-sm flex-1 min-w-[240px]"
          placeholder="Search signature, file, module, reason…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          className="border rounded px-3 py-1 text-sm"
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value)}
        >
          <option value="">All kinds</option>
          {kinds.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </div>

      {error ? (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded p-3 text-sm">
          {error}
        </div>
      ) : null}

      <div className="bg-white border rounded shadow-sm overflow-auto">
        <table className="min-w-full divide-y divide-gray-200 text-sm">
          <thead className="bg-gray-50 sticky top-0">
            <tr>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Kind</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Signature</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Module</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">File:Line</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Reason</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {loading ? (
              <tr>
                <td colSpan={6} className="px-4 py-6 text-center text-gray-500">
                  Loading…
                </td>
              </tr>
            ) : filtered.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-4 py-6 text-center text-gray-500">
                  No source points match.
                </td>
              </tr>
            ) : (
              filtered.map((p) => (
                <tr key={p.id} className="hover:bg-gray-50 align-top">
                  <td className="px-4 py-2">
                    <span className="inline-block px-2 py-0.5 rounded bg-blue-50 text-blue-700 text-xs">
                      {p.kind}
                    </span>
                  </td>
                  <td className="px-4 py-2 font-mono text-xs">{p.signature}</td>
                  <td className="px-4 py-2 text-gray-600">{p.module}</td>
                  <td className="px-4 py-2 font-mono text-xs text-gray-600">
                    {p.file}:{p.line}
                  </td>
                  <td className="px-4 py-2 text-gray-600">{p.reason}</td>
                  <td className="px-4 py-2">
                    <Link
                      to={`/graph?source=${encodeURIComponent(p.id)}`}
                      className="text-blue-600 hover:underline text-xs"
                    >
                      Browse →
                    </Link>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
