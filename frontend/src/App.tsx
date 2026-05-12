import { useCallback, useEffect, useState } from 'react';
import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom';
import Dashboard from './pages/Dashboard';
import SourcePointList from './pages/SourcePointList';
import FunctionBrowser from './pages/FunctionBrowser';
import CallGraphView from './pages/CallGraphView';
import ReviewQueue from './pages/ReviewQueue';
import FeedbackLog from './pages/FeedbackLog';
import { api, type Stats } from './api/client';

/**
 * Rendered chip spec derived from a `/api/v1/stats` snapshot. Returning
 * `null` hides the chip entirely (e.g. while stats are still loading or
 * a badge is not meaningful). `tone` drives the color:
 *
 * - `default` — gray pill (baseline / zero backlog)
 * - `warn`    — amber pill (attention but not urgent)
 * - `alert`   — red pill (actively demands review)
 *
 * `to` optionally overrides the NavLink target so warn/alert chips can
 * deep-link into the pre-filtered sub-view (architecture.md §5 跨页面
 * drill-down 契约). When omitted, the nav item's static `path` wins.
 *
 * Kept as a pure derivation so the nav poller stays thin and each nav
 * item owns its own "what does the number mean" logic.
 */
type BadgeSpec = {
  count: number;
  tone: 'default' | 'warn' | 'alert';
  title: string;
  to?: string;
};

type NavItem = {
  path: string;
  label: string;
  deriveBadge?: (stats: Stats) => BadgeSpec | null;
};

const TONE_CLASSES: Record<BadgeSpec['tone'], string> = {
  default: 'bg-gray-100 text-gray-500',
  warn: 'bg-amber-100 text-amber-800',
  alert: 'bg-red-100 text-red-800',
};

const navItems: NavItem[] = [
  { path: '/', label: 'Dashboard' },
  { path: '/sources', label: 'Source Points' },
  { path: '/functions', label: 'Functions' },
  { path: '/graph', label: 'Call Graph' },
  {
    path: '/review',
    label: 'Review',
    // Review backlog chip (architecture.md §3 UnresolvedCall 生命周期:
    // pending → Agent fix / 3 retries → "unresolvable"). Alert tone
    // mirrors the Dashboard StatCard when the agent has abandoned a
    // GAP (any unresolvable > 0), otherwise warn when there's still a
    // pending queue, otherwise gray. Keeps reviewers on other pages
    // aware of the backlog without mounting ReviewQueue
    // (北极星指标 #5 状态透明度 + 候选优化方向 #4 进度与可观测性).
    deriveBadge: (s) => {
      const byStatus = s.unresolved_by_status ?? {};
      const unresolvable = byStatus.unresolvable ?? 0;
      // Fall back to total_unresolved when the bucket field is missing
      // (older backend stubs without unresolved_by_status) so the chip
      // still surfaces a non-zero count instead of silently hiding.
      const pending =
        byStatus.pending ??
        Math.max(0, (s.total_unresolved ?? 0) - unresolvable);
      const total = pending + unresolvable;
      if (unresolvable > 0) {
        return {
          count: total,
          tone: 'alert',
          // Auto drill-down: when the agent has abandoned a GAP we want
          // "see red chip → 1 click → land on the abandoned list" to
          // hold from any page, not just from the Dashboard StatCard
          // (architecture.md §5 跨页面 drill-down 契约).
          to: '/review?status=unresolvable',
          title: `${pending} pending · ${unresolvable} unresolvable — agent gave up on ${unresolvable} GAP${
            unresolvable === 1 ? '' : 's'
          }`,
        };
      }
      if (pending > 0) {
        return {
          count: total,
          tone: 'warn',
          to: '/review?status=pending',
          title: `${pending} pending GAP${pending === 1 ? '' : 's'} awaiting repair`,
        };
      }
      return { count: 0, tone: 'default', title: 'No outstanding GAPs' };
    },
  },
  {
    path: '/feedback',
    label: 'Feedback',
    // architecture.md §3 反馈机制 + §8 — `total_feedback` surfaces
    // counter-example library growth (北极星指标 #5 + 候选优化方向 #4).
    deriveBadge: (s) => {
      const count = s.total_feedback ?? 0;
      return {
        count,
        tone: count === 0 ? 'default' : 'warn',
        title: `${count} counter example${count === 1 ? '' : 's'} in library`,
      };
    },
  },
];

export default function App() {
  // Shared stats poller for nav badges. Dashboard has its own 2s poller
  // for the stat cards, so we stay at 5s here to avoid doubling load
  // while still feeling "live" to reviewers watching the library grow.
  const [stats, setStats] = useState<Stats | null>(null);

  const refreshStats = useCallback(async () => {
    try {
      const s = await api.getStats();
      setStats(s);
    } catch {
      // Silent — nav chips are non-blocking UI. Failed polls keep the
      // last-known snapshot rather than flipping to zero and alarming
      // the reviewer about an outage that a richer component
      // (Dashboard) will already surface.
    }
  }, []);

  useEffect(() => {
    refreshStats();
    const id = setInterval(refreshStats, 5000);
    return () => clearInterval(id);
  }, [refreshStats]);

  return (
    <BrowserRouter>
      <div className="min-h-screen bg-gray-100">
        <nav className="bg-white shadow-sm border-b">
          <div className="max-w-7xl mx-auto px-4">
            <div className="flex items-center h-14 gap-6">
              <span className="font-bold text-lg">codemap-lite</span>
              {navItems.map((item) => {
                const badge =
                  item.deriveBadge && stats ? item.deriveBadge(stats) : null;
                // Nav chip may override the NavLink target so warn/alert
                // chips deep-link into pre-filtered sub-views; default
                // tone keeps the static path (architecture.md §5).
                const to = badge?.to ?? item.path;
                return (
                  <NavLink
                    key={item.path}
                    to={to}
                    className={({ isActive }) =>
                      `text-sm inline-flex items-center gap-1.5 ${
                        isActive
                          ? 'text-blue-600 font-medium'
                          : 'text-gray-600 hover:text-gray-900'
                      }`
                    }
                  >
                    <span>{item.label}</span>
                    {badge ? (
                      <span
                        className={`inline-flex items-center justify-center min-w-[1.25rem] px-1.5 rounded-full text-[11px] font-semibold leading-[1.125rem] ${TONE_CLASSES[badge.tone]}`}
                        title={badge.title}
                      >
                        {badge.count}
                      </span>
                    ) : null}
                  </NavLink>
                );
              })}
            </div>
          </div>
        </nav>
        <main className="max-w-7xl mx-auto">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/sources" element={<SourcePointList />} />
            <Route path="/functions" element={<FunctionBrowser />} />
            <Route path="/graph" element={<CallGraphView />} />
            <Route path="/review" element={<ReviewQueue />} />
            <Route path="/feedback" element={<FeedbackLog />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}
