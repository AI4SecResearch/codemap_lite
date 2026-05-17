import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom';
import { LayoutDashboard, Target, Code2, GitFork, MessageSquareWarning, Bot } from 'lucide-react';
import Dashboard from './pages/Dashboard';
import SourcePointList from './pages/SourcePointList';
import FunctionBrowser from './pages/FunctionBrowser';
import FeedbackLog from './pages/FeedbackLog';
import RepairActivity from './pages/RepairActivity';
import { api, type Stats } from './api/client';

const CallGraphView = lazy(() => import('./pages/CallGraphView'));

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
  icon: React.ReactNode;
  deriveBadge?: (stats: Stats) => BadgeSpec | null;
};

const TONE_CLASSES: Record<BadgeSpec['tone'], string> = {
  default: 'bg-gray-100 text-gray-500',
  warn: 'bg-amber-100 text-amber-800',
  alert: 'bg-red-100 text-red-800',
};

const navItems: NavItem[] = [
  { path: '/', label: 'Dashboard', icon: <LayoutDashboard className="w-4 h-4" /> },
  {
    path: '/sources',
    label: 'Sources',
    icon: <Target className="w-4 h-4" />,
    // Source points badge: show total source points count with status breakdown
    deriveBadge: (s) => {
      const spByStatus = s.source_points_by_status ?? {};
      const total = s.total_source_points ?? 0;
      const pending = spByStatus.pending ?? 0;
      const running = spByStatus.running ?? 0;
      const complete = spByStatus.complete ?? 0;
      if (total === 0) return { count: 0, tone: 'default', title: 'No source points' };
      if (running > 0) {
        return {
          count: total,
          tone: 'warn',
          title: `${complete} complete · ${running} running · ${pending} pending`,
        };
      }
      if (pending > 0) {
        return {
          count: total,
          tone: 'warn',
          title: `${complete} complete · ${pending} pending`,
        };
      }
      return { count: total, tone: 'default', title: `${total} source points — all complete` };
    },
  },
  { path: '/functions', label: 'Functions', icon: <Code2 className="w-4 h-4" /> },
  { path: '/repairs', label: 'Repairs', icon: <Bot className="w-4 h-4" />,
    deriveBadge: (s) => {
      const llm = s.total_llm_edges ?? 0;
      const logs = s.total_repair_logs ?? 0;
      if (logs > 0) return { count: logs, tone: 'warn' as const, title: `${logs} repair log${logs === 1 ? '' : 's'} · ${llm} LLM edge${llm === 1 ? '' : 's'}` };
      return { count: 0, tone: 'default' as const, title: 'No repairs yet' };
    },
  },
  { path: '/graph', label: 'Call Graph', icon: <GitFork className="w-4 h-4" /> },
  {
    path: '/feedback',
    label: 'Feedback',
    icon: <MessageSquareWarning className="w-4 h-4" />,
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
      <div className="min-h-screen bg-gray-50">
        <nav className="bg-white shadow-card border-b border-gray-200 sticky top-0 z-40">
          <div className="max-w-7xl mx-auto px-4">
            <div className="flex items-center h-14 gap-1">
              <span className="font-bold text-lg mr-6 bg-gradient-to-r from-blue-600 to-indigo-600 bg-clip-text text-transparent">codemap-lite</span>
              {navItems.map((item) => {
                const badge =
                  item.deriveBadge && stats ? item.deriveBadge(stats) : null;
                const to = badge?.to ?? item.path;
                return (
                  <NavLink
                    key={item.path}
                    to={to}
                    className={({ isActive }) =>
                      `text-sm inline-flex items-center gap-1.5 px-3 py-2 rounded-lg transition-all duration-200 ${
                        isActive
                          ? 'text-blue-600 font-medium bg-blue-50'
                          : 'text-gray-600 hover:text-gray-900 hover:bg-gray-100'
                      }`
                    }
                  >
                    {item.icon}
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
            <Route path="/repairs" element={<RepairActivity />} />
            <Route path="/graph" element={<Suspense fallback={<div className="p-6 text-gray-500 text-sm">Loading graph view…</div>}><CallGraphView /></Suspense>} />
            <Route path="/feedback" element={<FeedbackLog />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}
