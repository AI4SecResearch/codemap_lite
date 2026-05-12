import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  // When non-null, the "Mark wrong" modal is open for this gap — the user
  // fills the correct target + generalized pattern that will be POSTed to
  // /api/v1/feedback (architecture.md §5 审阅标记错误时).
  const [wrongFor, setWrongFor] = useState<UnresolvedCall | null>(null);
  const tableRef = useRef<HTMLDivElement | null>(null);

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

  // Clamp selectedIndex when the filtered list shrinks/grows.
  useEffect(() => {
    if (selectedIndex === null) return;
    if (filteredGaps.length === 0) {
      setSelectedIndex(null);
    } else if (selectedIndex >= filteredGaps.length) {
      setSelectedIndex(filteredGaps.length - 1);
    }
  }, [filteredGaps, selectedIndex]);

  // Scroll the selected row into view inside the scrollable table container.
  useEffect(() => {
    if (selectedIndex === null) return;
    const container = tableRef.current;
    if (!container) return;
    const row = container.querySelector<HTMLTableRowElement>(
      `tr[data-row-index="${selectedIndex}"]`
    );
    row?.scrollIntoView({ block: 'nearest' });
  }, [selectedIndex]);

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
    // Open the counter-example modal — the actual POST happens in
    // submitWrong(). Architecture.md §5 requires the reviewer to supply
    // a correct target before we generate a counter example.
    setWrongFor(g);
  };

  const submitWrong = async (
    g: UnresolvedCall,
    correctTarget: string,
    pattern: string
  ) => {
    const key = g.id ?? `${g.caller_id}:${g.call_line}`;
    setBusyId(key);
    try {
      // Persist the counter example so the next repair round picks it up
      // via RepairOrchestrator (architecture.md §3 反馈机制 step 4).
      await api.createFeedback({
        call_context: g.call_expression,
        wrong_target: g.candidates?.[0] ?? '(unknown)',
        correct_target: correctTarget,
        pattern: pattern || correctTarget,
      });
      // Also record the reviewer's decision as a Review — preserves the
      // existing audit trail surfaced in the Reviews tab.
      await api.createReview({
        function_id: g.caller_id,
        comment: `marked wrong: ${g.call_expression} → ${correctTarget}`,
        status: 'rejected',
      });
      setWrongFor(null);
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

  // Keyboard shortcuts for the Unresolved GAPs tab.
  // j/↓ next · k/↑ prev · y = mark correct · n = mark wrong · Esc = clear selection.
  useEffect(() => {
    if (tab !== 'gaps') return;

    const handler = (e: KeyboardEvent) => {
      // Never hijack keys while the user is typing into an input/textarea.
      const target = e.target as HTMLElement | null;
      if (target) {
        const tag = target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || target.isContentEditable) {
          return;
        }
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (filteredGaps.length === 0) return;

      const move = (delta: number) => {
        setSelectedIndex((cur) => {
          if (cur === null) return delta > 0 ? 0 : filteredGaps.length - 1;
          const next = cur + delta;
          if (next < 0) return 0;
          if (next >= filteredGaps.length) return filteredGaps.length - 1;
          return next;
        });
      };

      switch (e.key) {
        case 'j':
        case 'ArrowDown':
          e.preventDefault();
          move(1);
          break;
        case 'k':
        case 'ArrowUp':
          e.preventDefault();
          move(-1);
          break;
        case 'y': {
          if (selectedIndex === null) return;
          const g = filteredGaps[selectedIndex];
          if (!g) return;
          e.preventDefault();
          void markCorrect(g);
          break;
        }
        case 'n': {
          if (selectedIndex === null) return;
          const g = filteredGaps[selectedIndex];
          if (!g) return;
          e.preventDefault();
          void markWrong(g);
          break;
        }
        case 'Escape':
          if (selectedIndex !== null) {
            e.preventDefault();
            setSelectedIndex(null);
          }
          break;
      }
    };

    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [tab, filteredGaps, selectedIndex, markCorrect, markWrong]);

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
          <div className="text-xs text-gray-500 flex flex-wrap gap-x-3 gap-y-1">
            <span>
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">j</kbd>/
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">↓</kbd> next
            </span>
            <span>
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">k</kbd>/
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">↑</kbd> prev
            </span>
            <span>
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">y</kbd> mark correct
            </span>
            <span>
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">n</kbd> mark wrong
            </span>
            <span>
              <kbd className="px-1 py-0.5 border rounded bg-gray-50">Esc</kbd> clear
            </span>
            <span className="text-gray-400">
              (disabled while typing in the filter box · selected row expands with call context)
            </span>
          </div>
          <div
            ref={tableRef}
            className="bg-white border rounded shadow-sm overflow-auto max-h-[70vh]"
          >
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
                    const selected = i === selectedIndex;
                    return (
                      <Fragment key={key}>
                      <tr
                        data-row-index={i}
                        onClick={() => setSelectedIndex(i)}
                        className={`align-top cursor-pointer ${
                          selected
                            ? 'bg-blue-50 ring-2 ring-inset ring-blue-400'
                            : 'hover:bg-gray-50'
                        }`}
                      >
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
                      {selected ? (
                        <tr className="bg-blue-50/60">
                          <td colSpan={6} className="px-3 pb-3 pt-0">
                            <GapDetail gap={g} />
                          </td>
                        </tr>
                      ) : null}
                      </Fragment>
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
      {wrongFor ? (
        <MarkWrongModal
          gap={wrongFor}
          busy={
            busyId === (wrongFor.id ?? `${wrongFor.caller_id}:${wrongFor.call_line}`)
          }
          onCancel={() => setWrongFor(null)}
          onSubmit={(correct, pattern) => submitWrong(wrongFor, correct, pattern)}
        />
      ) : null}
    </div>
  );
}

function MarkWrongModal({
  gap,
  busy,
  onCancel,
  onSubmit,
}: {
  gap: UnresolvedCall;
  busy: boolean;
  onCancel: () => void;
  onSubmit: (correctTarget: string, pattern: string) => void;
}) {
  const [correctTarget, setCorrectTarget] = useState('');
  const [pattern, setPattern] = useState('');

  const canSubmit = correctTarget.trim().length > 0 && !busy;

  return (
    <div
      className="fixed inset-0 bg-black/40 flex items-center justify-center p-4 z-50"
      onClick={onCancel}
    >
      <div
        className="bg-white rounded shadow-lg w-full max-w-md p-4 space-y-3"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold">Mark as wrong — add counter example</h2>
        <p className="text-xs text-gray-500">
          Architecture &sect;5: fill the correct target so the next repair
          round avoids this mistake. The pattern (generalized rule) is
          injected into the agent&rsquo;s <code>CLAUDE.md</code>.
        </p>

        <dl className="text-xs bg-gray-50 border rounded p-2 space-y-1">
          <div className="flex gap-2">
            <dt className="text-gray-500 w-24 shrink-0">call</dt>
            <dd className="font-mono break-all">{gap.call_expression}</dd>
          </div>
          <div className="flex gap-2">
            <dt className="text-gray-500 w-24 shrink-0">wrong target</dt>
            <dd className="font-mono break-all text-red-700">
              {gap.candidates?.[0] ?? '(unknown)'}
            </dd>
          </div>
        </dl>

        <label className="block text-sm">
          <span className="text-gray-700">Correct target</span>
          <input
            autoFocus
            className="mt-1 w-full border rounded px-2 py-1 text-sm font-mono"
            placeholder="e.g. modern_handler"
            value={correctTarget}
            onChange={(e) => setCorrectTarget(e.target.value)}
          />
        </label>

        <label className="block text-sm">
          <span className="text-gray-700">
            Generalized pattern{' '}
            <span className="text-gray-400">(optional — defaults to correct target)</span>
          </span>
          <textarea
            className="mt-1 w-full border rounded px-2 py-1 text-sm"
            rows={2}
            placeholder="e.g. dispatcher vtable resolution must prefer modern_handler"
            value={pattern}
            onChange={(e) => setPattern(e.target.value)}
          />
        </label>

        <div className="flex justify-end gap-2 pt-1">
          <button
            className="px-3 py-1 rounded border text-sm hover:bg-gray-50"
            onClick={onCancel}
            disabled={busy}
          >
            Cancel
          </button>
          <button
            className="px-3 py-1 rounded bg-red-600 text-white text-sm hover:bg-red-700 disabled:opacity-50"
            onClick={() => onSubmit(correctTarget.trim(), pattern.trim())}
            disabled={!canSubmit}
          >
            {busy ? 'Saving…' : 'Save counter example'}
          </button>
        </div>
      </div>
    </div>
  );
}

function GapDetail({ gap }: { gap: UnresolvedCall }) {
  const snippet = gap.source_code_snippet?.trim();
  const chips: { label: string; value: string; tone: string }[] = [];
  if (gap.var_name) {
    chips.push({ label: 'var', value: gap.var_name, tone: 'bg-slate-100 text-slate-700' });
  }
  if (gap.var_type) {
    chips.push({ label: 'type', value: gap.var_type, tone: 'bg-slate-100 text-slate-700' });
  }
  if (typeof gap.retry_count === 'number') {
    chips.push({
      label: 'retries',
      value: String(gap.retry_count),
      tone: gap.retry_count > 0 ? 'bg-amber-100 text-amber-700' : 'bg-gray-100 text-gray-600',
    });
  }
  if (gap.status) {
    const tone =
      gap.status === 'resolved'
        ? 'bg-green-100 text-green-700'
        : gap.status === 'failed'
        ? 'bg-red-100 text-red-700'
        : 'bg-gray-100 text-gray-700';
    chips.push({ label: 'status', value: gap.status, tone });
  }

  return (
    <div className="rounded border border-blue-200 bg-white p-3 space-y-2">
      {chips.length > 0 ? (
        <div className="flex flex-wrap gap-1 text-xs">
          {chips.map((c) => (
            <span
              key={`${c.label}:${c.value}`}
              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded ${c.tone}`}
            >
              <span className="text-[10px] uppercase tracking-wide opacity-70">{c.label}</span>
              <span className="font-mono break-all">{c.value}</span>
            </span>
          ))}
        </div>
      ) : null}
      {snippet ? (
        <pre className="text-xs font-mono bg-gray-50 border rounded p-2 overflow-x-auto whitespace-pre">
          {snippet}
        </pre>
      ) : (
        <div className="text-xs text-gray-400 italic">No source snippet captured for this call.</div>
      )}
      {gap.candidates && gap.candidates.length > 0 ? (
        <div className="text-xs text-gray-600">
          <div className="font-medium text-gray-700 mb-1">
            Candidates ({gap.candidates.length})
          </div>
          <ul className="pl-4 list-disc space-y-0.5">
            {gap.candidates.slice(0, 20).map((c) => (
              <li key={c} className="font-mono break-all">
                {c}
              </li>
            ))}
            {gap.candidates.length > 20 ? (
              <li className="text-gray-500">…and {gap.candidates.length - 20} more</li>
            ) : null}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
