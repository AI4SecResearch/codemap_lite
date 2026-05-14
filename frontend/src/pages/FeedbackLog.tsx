import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, CounterExample } from '../api/client';

/**
 * FeedbackLog — browses counter examples persisted by the backend
 * FeedbackStore (architecture.md §3 反馈机制). Surfaces the structured
 * CounterExample fields — pattern, call_context, wrong_target,
 * correct_target — instead of dumping raw JSON, advancing candidate
 * optimisation #5 (反例可视化) in CLAUDE.md.
 *
 * Deep-link: architecture.md §5 跨页面 drill-down 契约. Reads optional
 * `?pattern=<encoded>` query param on mount (and whenever it changes),
 * locates the first CounterExample whose `pattern` matches exactly,
 * applies a ring/blue highlight and scrolls it into view so reviewers
 * arriving from the ReviewQueue "saved" banner can confirm at a glance
 * that the pattern is persisted and will be injected into the next
 * repair round's CLAUDE.md (北极星 #5).
 */
export default function FeedbackLog() {
  const [items, setItems] = useState<CounterExample[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const highlightPattern = searchParams.get('pattern');
  const cardRefs = useRef<Map<string, HTMLElement>>(new Map());

  const refresh = async () => {
    setLoading(true);
    try {
      const data = await api.getFeedback();
      setItems(data.items);
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

  // Resolve highlight key to the actual matching index — highlight at
  // most one card, prefer the first pattern match. Memoized so the
  // effect below only triggers when data or the target pattern change.
  const highlightedKey = useMemo(() => {
    if (!highlightPattern) return null;
    const idx = items.findIndex((it) => it.pattern === highlightPattern);
    return idx >= 0 ? `${items[idx].pattern}-${idx}` : null;
  }, [items, highlightPattern]);

  useEffect(() => {
    if (!highlightedKey) return;
    const el = cardRefs.current.get(highlightedKey);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, [highlightedKey]);

  const clearHighlight = () => {
    const next = new URLSearchParams(searchParams);
    next.delete('pattern');
    setSearchParams(next, { replace: true });
  };

  // If the URL names a pattern that isn't in the library (stale link /
  // store reset), tell the reviewer rather than silently showing nothing.
  const missingHighlight =
    highlightPattern && !loading && !highlightedKey;

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
        Generalized counter examples from incorrect repairs. The{' '}
        <span className="font-semibold">pattern</span> is injected into the
        next repair round&rsquo;s <code>CLAUDE.md</code> so the agent can avoid
        repeating the same mistake.
      </p>

      {highlightPattern ? (
        <div
          className={`flex items-start justify-between gap-3 rounded border p-3 text-sm ${
            missingHighlight
              ? 'bg-amber-50 border-amber-200 text-amber-800'
              : 'bg-blue-50 border-blue-200 text-blue-800'
          }`}
        >
          <div className="space-y-0.5">
            <div className="font-medium">
              {missingHighlight
                ? 'Pattern not found in current library'
                : 'Highlighting pattern from ReviewQueue'}
            </div>
            <div className="text-xs font-mono break-all opacity-80">
              {highlightPattern}
            </div>
          </div>
          <button
            className="text-xs underline opacity-70 hover:opacity-100 shrink-0"
            onClick={clearHighlight}
          >
            Clear highlight
          </button>
        </div>
      ) : null}

      {error ? (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded p-3 text-sm">
          {error}
        </div>
      ) : null}

      {loading ? (
        <div className="text-gray-500 text-sm">Loading&hellip;</div>
      ) : items.length === 0 ? (
        <div className="bg-white border rounded p-6 text-sm text-gray-500">
          No counter examples yet. They appear here after a human marks a
          repair as wrong and the system generalizes a rule.
        </div>
      ) : (
        <div className="space-y-3">
          <div className="text-xs text-gray-500">
            {items.length} pattern{items.length === 1 ? '' : 's'} in the
            feedback store.
          </div>
          {items.map((item, i) => {
            const key = `${item.pattern}-${i}`;
            const isHighlighted = key === highlightedKey;
            return (
              <article
                key={key}
                ref={(el) => {
                  if (el) cardRefs.current.set(key, el);
                  else cardRefs.current.delete(key);
                }}
                className={`bg-white border rounded p-4 space-y-3 transition-shadow ${
                  isHighlighted
                    ? 'ring-2 ring-blue-400 border-blue-400 shadow'
                    : ''
                }`}
              >
                <header className="flex items-start gap-2">
                  <span className="inline-block shrink-0 px-2 py-0.5 rounded bg-amber-100 text-amber-800 text-xs font-semibold">
                    pattern
                  </span>
                  <h2 className="text-sm font-semibold text-gray-900 leading-snug">
                    {item.pattern}
                  </h2>
                </header>

                <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 text-xs">
                  <dt className="text-gray-500">call context</dt>
                  <dd>
                    <code className="font-mono bg-gray-50 border rounded px-1.5 py-0.5 break-all">
                      {item.call_context}
                    </code>
                  </dd>

                  <dt className="text-gray-500">wrong target</dt>
                  <dd>
                    <code className="font-mono bg-red-50 text-red-700 border border-red-200 rounded px-1.5 py-0.5 break-all">
                      {item.wrong_target}
                    </code>
                  </dd>

                  <dt className="text-gray-500">correct target</dt>
                  <dd>
                    <code className="font-mono bg-emerald-50 text-emerald-700 border border-emerald-200 rounded px-1.5 py-0.5 break-all">
                      {item.correct_target}
                    </code>
                  </dd>
                </dl>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
