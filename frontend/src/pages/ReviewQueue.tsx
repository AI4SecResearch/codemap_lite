import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, Review, UnresolvedCall } from '../api/client';

type Tab = 'gaps' | 'reviews';

function shorten(path: string): string {
  const parts = path.split('/');
  return parts.slice(-3).join('/');
}

export default function ReviewQueue() {
  const [tab, setTab] = useState<Tab>('gaps');
  const [gaps, setGaps] = useState<UnresolvedCall[]>([]);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [u, r] = await Promise.all([
        api.listUnresolved(500, 0),
        api.getReviews(),
      ]);
      setGaps(u.items);
      setReviews(r);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const filteredGaps = useMemo(() => {
    const term = filter.trim().toLowerCase();
    if (!term) return gaps;
    return gaps.filter(
      (g) =>
        g.caller_id.toLowerCase().includes(term) ||
        g.call_expression.toLowerCase().includes(term) ||
        g.call_type.toLowerCase().includes(term) ||
        (g.var_name ?? '').toLowerCase().includes(term)
    );
  }, [gaps, filter]);

  const markCorrect = async (g: UnresolvedCall) => {
    const key = g.id ?? `${g.caller_id}:${g.call_line}`;
    setBusyId(key);
    try {
      await api.createReview({
        function_id: g.caller_id,
        comment: `marked correct: ${g.call_expression}`,
        status: 'approved',
      });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const markWrong = async (g: UnresolvedCall) => {
    const key = g.id ?? `${g.caller_id}:${g.call_line}`;
    setBusyId(key);
    try {
      await api.createReview({
        function_id: g.caller_id,
        comment: `marked wrong: ${g.call_expression} — trigger counter-example`,
        status: 'rejected',
      });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const deleteReview = async (id: string) => {
    setBusyId(id);
    try {
      await api.deleteReview(id);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Review Queue</h1>
        <button
          className="px-3 py-1 rounded border text-sm hover:bg-gray-50"
          onClick={refresh}
          disabled={loading}
        >
          Refresh
        </button>
      </div>

      <div className="flex gap-2 border-b">
        <button
          className={`px-3 py-1 text-sm ${
            tab === 'gaps'
              ? 'border-b-2 border-blue-600 text-blue-600'
              : 'text-gray-600'
          }`}
          onClick={() => setTab('gaps')}
        >
          Unresolved GAPs ({gaps.length})
        </button>
        <button
          className={`px-3 py-1 text-sm ${
            tab === 'reviews'
              ? 'border-b-2 border-blue-600 text-blue-600'
              : 'text-gray-600'
          }`}
          onClick={() => setTab('reviews')}
        >
          Reviews ({reviews.length})
        </button>
      </div>

      {error ? (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded p-3 text-sm">
          {error}
        </div>
      ) : null}

      {tab === 'gaps' ? (
        <>
          <input
            className="w-full border rounded px-3 py-1 text-sm"
            placeholder="Filter by caller, expression, call_type, var_name…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <div className="bg-white border rounded shadow-sm overflow-auto">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50 sticky top-0">
                <tr>
                  <th className="px-3 py-2 text-left font-medium text-gray-500">Type</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-500">Expression</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-500">Caller</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-500">Location</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-500">Candidates</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-500">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {loading ? (
                  <tr>
                    <td colSpan={6} className="px-3 py-6 text-center text-gray-500">
                      Loading…
                    </td>
                  </tr>
                ) : filteredGaps.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="px-3 py-6 text-center text-gray-500">
                      No unresolved GAPs.
                    </td>
                  </tr>
                ) : (
                  filteredGaps.map((g, i) => {
                    const key = g.id ?? `${g.caller_id}:${g.call_line}:${i}`;
                    const busy = busyId === key;
                    return (
                      <tr key={key} className="hover:bg-gray-50 align-top">
                        <td className="px-3 py-2">
                          <span className="inline-block px-2 py-0.5 rounded bg-amber-50 text-amber-700 text-xs">
                            {g.call_type}
                          </span>
                        </td>
                        <td className="px-3 py-2 font-mono text-xs">
                          {g.call_expression}
                        </td>
                        <td
                          className="px-3 py-2 font-mono text-xs text-gray-600"
                          title={g.caller_id}
                        >
                          {shorten(g.caller_id)}
                        </td>
                        <td className="px-3 py-2 font-mono text-xs text-gray-600">
                          {shorten(g.call_file)}:{g.call_line}
                        </td>
                        <td className="px-3 py-2 text-xs text-gray-600">
                          {g.candidates && g.candidates.length > 0 ? (
                            <details>
                              <summary>{g.candidates.length} candidates</summary>
                              <ul className="mt-1 pl-4 list-disc">
                                {g.candidates.slice(0, 10).map((c) => (
                                  <li key={c} className="font-mono break-all">
                                    {c}
                                  </li>
                                ))}
                                {g.candidates.length > 10 ? (
                                  <li>…and {g.candidates.length - 10} more</li>
                                ) : null}
                              </ul>
                            </details>
                          ) : (
                            <span className="text-gray-400">—</span>
                          )}
                        </td>
                        <td className="px-3 py-2">
                          <div className="flex gap-1">
                            <button
                              className="px-2 py-0.5 rounded bg-green-100 text-green-700 text-xs hover:bg-green-200 disabled:opacity-50"
                              onClick={() => markCorrect(g)}
                              disabled={busy}
                            >
                              Mark correct
                            </button>
                            <button
                              className="px-2 py-0.5 rounded bg-red-100 text-red-700 text-xs hover:bg-red-200 disabled:opacity-50"
                              onClick={() => markWrong(g)}
                              disabled={busy}
                              title="Trigger counter-example generation"
                            >
                              Mark wrong
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <div className="bg-white border rounded shadow-sm overflow-auto">
          <table className="min-w-full divide-y divide-gray-200 text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-left font-medium text-gray-500">Status</th>
                <th className="px-3 py-2 text-left font-medium text-gray-500">Function</th>
                <th className="px-3 py-2 text-left font-medium text-gray-500">Comment</th>
                <th className="px-3 py-2 text-left font-medium text-gray-500">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {reviews.length === 0 ? (
                <tr>
                  <td colSpan={4} className="px-3 py-6 text-center text-gray-500">
                    No reviews yet.
                  </td>
                </tr>
              ) : (
                reviews.map((r) => (
                  <tr key={r.id} className="hover:bg-gray-50 align-top">
                    <td className="px-3 py-2">
                      <span
                        className={`inline-block px-2 py-0.5 rounded text-xs ${
                          r.status === 'approved'
                            ? 'bg-green-100 text-green-700'
                            : r.status === 'rejected'
                            ? 'bg-red-100 text-red-700'
                            : 'bg-gray-100 text-gray-700'
                        }`}
                      >
                        {r.status}
                      </span>
                    </td>
                    <td
                      className="px-3 py-2 font-mono text-xs text-gray-600"
                      title={r.function_id}
                    >
                      {shorten(r.function_id)}
                    </td>
                    <td className="px-3 py-2 text-xs text-gray-700">
                      {r.comment}
                    </td>
                    <td className="px-3 py-2">
                      <button
                        className="text-xs text-red-600 hover:underline disabled:opacity-50"
                        onClick={() => deleteReview(r.id)}
                        disabled={busyId === r.id}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
