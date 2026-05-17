import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { RefreshCw, BookOpen, Trash2, Pencil, Save, X } from 'lucide-react';
import { api, CounterExample } from '../api/client';
import { Button, EmptyState, Skeleton, SearchInput, ConfirmDialog } from '../components/ui';

/**
 * FeedbackLog — browses counter examples persisted by the backend
 * FeedbackStore (architecture.md §3 反馈机制).
 */

const BORDER_COLORS = [
  'border-l-blue-500',
  'border-l-purple-500',
  'border-l-amber-500',
  'border-l-green-500',
  'border-l-red-500',
  'border-l-sky-500',
  'border-l-fuchsia-500',
];

function patternColor(pattern: string): string {
  let hash = 0;
  for (let i = 0; i < pattern.length; i++) hash = ((hash << 5) - hash + pattern.charCodeAt(i)) | 0;
  return BORDER_COLORS[Math.abs(hash) % BORDER_COLORS.length];
}

export default function FeedbackLog() {
  const [items, setItems] = useState<CounterExample[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const highlightPattern = searchParams.get('pattern');
  const cardRefs = useRef<Map<string, HTMLElement>>(new Map());
  const [search, setSearch] = useState('');
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [editForm, setEditForm] = useState<Partial<CounterExample>>({});
  const [deleteIdx, setDeleteIdx] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);

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

  useEffect(() => { refresh(); }, []);

  const filtered = useMemo(() => {
    if (!search) return items;
    const q = search.toLowerCase();
    return items.filter((it) => it.pattern.toLowerCase().includes(q) || it.call_context.toLowerCase().includes(q));
  }, [items, search]);

  const highlightedKey = useMemo(() => {
    if (!highlightPattern) return null;
    const idx = filtered.findIndex((it) => it.pattern === highlightPattern);
    return idx >= 0 ? `${filtered[idx].pattern}-${idx}` : null;
  }, [filtered, highlightPattern]);

  useEffect(() => {
    if (!highlightedKey) return;
    const el = cardRefs.current.get(highlightedKey);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [highlightedKey]);

  const clearHighlight = () => {
    const next = new URLSearchParams(searchParams);
    next.delete('pattern');
    setSearchParams(next, { replace: true });
  };

  const handleDelete = async () => {
    if (deleteIdx == null) return;
    setSaving(true);
    try {
      await api.deleteFeedback(deleteIdx);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setSaving(false);
    setDeleteIdx(null);
  };

  const handleSaveEdit = async () => {
    if (editingIdx == null) return;
    setSaving(true);
    try {
      await api.updateFeedback(editingIdx, editForm);
      await refresh();
      setEditingIdx(null);
      setEditForm({});
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setSaving(false);
  };

  const startEdit = (i: number, item: CounterExample) => {
    setEditingIdx(i);
    setEditForm({ pattern: item.pattern, call_context: item.call_context, wrong_target: item.wrong_target, correct_target: item.correct_target });
  };

  const missingHighlight = highlightPattern && !loading && !highlightedKey;

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900">Feedback Log</h1>
        <Button variant="secondary" size="sm" icon={<RefreshCw className="w-4 h-4" />} onClick={refresh} loading={loading}>Refresh</Button>
      </div>
      <p className="text-gray-500 text-sm">
        Counter-examples from incorrect repairs. Each <span className="font-semibold">pattern</span> is injected into the next repair round's <code className="bg-gray-100 px-1 rounded">CLAUDE.md</code> so the agent avoids repeating the same mistake.
      </p>

      <SearchInput value={search} onChange={setSearch} placeholder="Search patterns or call context…" className="max-w-md" />

      <ConfirmDialog
        open={deleteIdx != null}
        title="删除此反例？"
        description="删除后无法恢复，下一轮修复将不再注入此规则。"
        confirmLabel="删除"
        variant="danger"
        onConfirm={handleDelete}
        onCancel={() => setDeleteIdx(null)}
      />

      {highlightPattern && (
        <div className={`flex items-start justify-between gap-3 rounded-lg border p-3 text-sm animate-fade-in ${missingHighlight ? 'bg-amber-50 border-amber-200 text-amber-800' : 'bg-blue-50 border-blue-200 text-blue-800'}`}>
          <div className="space-y-0.5">
            <div className="font-medium">{missingHighlight ? 'Pattern not found in current library' : 'Highlighting pattern from Sources'}</div>
            <div className="text-xs font-mono break-all opacity-80">{highlightPattern}</div>
          </div>
          <button className="text-xs underline opacity-70 hover:opacity-100 shrink-0" onClick={clearHighlight}>Clear</button>
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm">{error}</div>
      )}

      {loading ? (
        <div className="space-y-3">
          <Skeleton className="h-24 w-full rounded-xl" />
          <Skeleton className="h-24 w-full rounded-xl" />
          <Skeleton className="h-24 w-full rounded-xl" />
        </div>
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={<BookOpen className="w-8 h-8 text-gray-400" />}
          title={search ? 'No matches' : 'No counter-examples yet'}
          description={search ? 'Try a different search term.' : 'They appear here after a reviewer marks a repair as wrong and the system generalizes a rule.'}
        />
      ) : (
        <div className="space-y-3">
          <div className="text-xs text-gray-500">{filtered.length} pattern{filtered.length === 1 ? '' : 's'}{search ? ' matching' : ' in the feedback store'}.</div>
          {filtered.map((item, i) => {
            const key = `${item.pattern}-${i}`;
            const isHighlighted = key === highlightedKey;
            const borderColor = patternColor(item.pattern);
            const isEditing = editingIdx === i;
            return (
              <article
                key={key}
                ref={(el) => { if (el) cardRefs.current.set(key, el); else cardRefs.current.delete(key); }}
                className={`bg-white border rounded-xl p-4 space-y-3 border-l-4 ${borderColor} transition-all duration-300 ${isHighlighted ? 'ring-2 ring-blue-400 shadow-card-hover animate-fade-in' : 'shadow-card hover:shadow-card-hover'}`}
              >
                <header className="flex items-start gap-2">
                  <span className="inline-block shrink-0 px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 text-xs font-semibold">pattern</span>
                  {isEditing ? (
                    <input className="flex-1 text-sm font-semibold border rounded px-2 py-1" value={editForm.pattern ?? ''} onChange={(e) => setEditForm({ ...editForm, pattern: e.target.value })} />
                  ) : (
                    <h2 className="text-sm font-semibold text-gray-900 leading-snug flex-1">{item.pattern}</h2>
                  )}
                  <div className="flex gap-1 shrink-0">
                    {isEditing ? (
                      <>
                        <Button variant="ghost" size="sm" icon={<Save className="w-3.5 h-3.5" />} onClick={handleSaveEdit} loading={saving}>Save</Button>
                        <Button variant="ghost" size="sm" icon={<X className="w-3.5 h-3.5" />} onClick={() => { setEditingIdx(null); setEditForm({}); }}>Cancel</Button>
                      </>
                    ) : (
                      <>
                        <Button variant="ghost" size="sm" icon={<Pencil className="w-3.5 h-3.5" />} onClick={() => startEdit(i, item)} />
                        <Button variant="ghost" size="sm" icon={<Trash2 className="w-3.5 h-3.5 text-red-500" />} onClick={() => setDeleteIdx(i)} />
                      </>
                    )}
                  </div>
                </header>
                {isEditing ? (
                  <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 text-xs">
                    <dt className="text-gray-500">call context</dt>
                    <dd><input className="w-full border rounded px-2 py-1 font-mono text-xs" value={editForm.call_context ?? ''} onChange={(e) => setEditForm({ ...editForm, call_context: e.target.value })} /></dd>
                    <dt className="text-gray-500">wrong target</dt>
                    <dd><input className="w-full border rounded px-2 py-1 font-mono text-xs" value={editForm.wrong_target ?? ''} onChange={(e) => setEditForm({ ...editForm, wrong_target: e.target.value })} /></dd>
                    <dt className="text-gray-500">correct target</dt>
                    <dd><input className="w-full border rounded px-2 py-1 font-mono text-xs" value={editForm.correct_target ?? ''} onChange={(e) => setEditForm({ ...editForm, correct_target: e.target.value })} /></dd>
                  </dl>
                ) : (
                  <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 text-xs">
                    <dt className="text-gray-500">call context</dt>
                    <dd><code className="font-mono bg-gray-50 border rounded px-1.5 py-0.5 break-all">{item.call_context}</code></dd>
                    <dt className="text-gray-500">wrong target</dt>
                    <dd><code className="font-mono bg-red-50 text-red-700 border border-red-200 rounded px-1.5 py-0.5 break-all">{item.wrong_target}</code></dd>
                    <dt className="text-gray-500">correct target</dt>
                    <dd><code className="font-mono bg-emerald-50 text-emerald-700 border border-emerald-200 rounded px-1.5 py-0.5 break-all">{item.correct_target || '(not specified)'}</code></dd>
                  </dl>
                )}
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
