import { useEffect, useState } from 'react';
import { api } from '../api/client';

export default function FeedbackLog() {
  const [items, setItems] = useState<unknown[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    try {
      const data = await api.getFeedback();
      setItems(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Feedback Log (Counter Examples)</h1>
        <button
          className="px-3 py-1 rounded border text-sm hover:bg-gray-50"
          onClick={refresh}
          disabled={loading}
        >
          Refresh
        </button>
      </div>
      <p className="text-gray-500 text-sm">
        Generalized counter examples from incorrect repairs.
      </p>

      {error ? (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded p-3 text-sm">
          {error}
        </div>
      ) : null}

      {loading ? (
        <div className="text-gray-500 text-sm">Loading…</div>
      ) : items.length === 0 ? (
        <div className="bg-white border rounded p-6 text-sm text-gray-500">
          No counter examples yet. They appear here after a human marks a
          repair as wrong and the system generalizes a rule.
        </div>
      ) : (
        <div className="space-y-3">
          {items.map((item, i) => (
            <pre
              key={i}
              className="bg-white border rounded p-3 text-xs overflow-auto"
            >
              {JSON.stringify(item, null, 2)}
            </pre>
          ))}
        </div>
      )}
    </div>
  );
}
