import { useCallback, useEffect, useState } from 'react';
import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom';
import Dashboard from './pages/Dashboard';
import SourcePointList from './pages/SourcePointList';
import FunctionBrowser from './pages/FunctionBrowser';
import CallGraphView from './pages/CallGraphView';
import ReviewQueue from './pages/ReviewQueue';
import FeedbackLog from './pages/FeedbackLog';
import { api } from './api/client';

/**
 * Nav item keyed off the shared /api/v1/stats payload so we can render
 * a live count chip on high-signal labels without giving every page its
 * own poller. Currently only "Feedback" uses `badgeKey` (architecture.md
 * §3 反馈机制 + §8 — `total_feedback` field surfaces counter-example
 * library growth; 北极星指标 #5 + 候选优化方向 #4). Extend the type if
 * more chips are added later (e.g. `total_unresolved` on Review).
 */
type NavItem = {
  path: string;
  label: string;
  badgeKey?: 'total_feedback';
};

const navItems: NavItem[] = [
  { path: '/', label: 'Dashboard' },
  { path: '/sources', label: 'Source Points' },
  { path: '/functions', label: 'Functions' },
  { path: '/graph', label: 'Call Graph' },
  { path: '/review', label: 'Review' },
  { path: '/feedback', label: 'Feedback', badgeKey: 'total_feedback' },
];

export default function App() {
  // Shared stats poller for nav badges. Dashboard has its own 2s poller
  // for the stat cards, so we stay at 5s here to avoid doubling load
  // while still feeling "live" to reviewers watching the library grow.
  const [badges, setBadges] = useState<Record<string, number>>({});

  const refreshBadges = useCallback(async () => {
    try {
      const s = await api.getStats();
      setBadges({ total_feedback: s.total_feedback ?? 0 });
    } catch {
      // Silent — nav chips are non-blocking UI. Failed polls keep the
      // last-known count rather than flipping to zero and alarming the
      // reviewer about an outage that a richer component (Dashboard)
      // will already surface.
    }
  }, []);

  useEffect(() => {
    refreshBadges();
    const id = setInterval(refreshBadges, 5000);
    return () => clearInterval(id);
  }, [refreshBadges]);

  return (
    <BrowserRouter>
      <div className="min-h-screen bg-gray-100">
        <nav className="bg-white shadow-sm border-b">
          <div className="max-w-7xl mx-auto px-4">
            <div className="flex items-center h-14 gap-6">
              <span className="font-bold text-lg">codemap-lite</span>
              {navItems.map((item) => {
                const count = item.badgeKey ? badges[item.badgeKey] : undefined;
                const hasCount = typeof count === 'number';
                return (
                  <NavLink
                    key={item.path}
                    to={item.path}
                    className={({ isActive }) =>
                      `text-sm inline-flex items-center gap-1.5 ${
                        isActive
                          ? 'text-blue-600 font-medium'
                          : 'text-gray-600 hover:text-gray-900'
                      }`
                    }
                  >
                    <span>{item.label}</span>
                    {hasCount ? (
                      <span
                        className={`inline-flex items-center justify-center min-w-[1.25rem] px-1.5 rounded-full text-[11px] font-semibold leading-[1.125rem] ${
                          count === 0
                            ? 'bg-gray-100 text-gray-500'
                            : 'bg-amber-100 text-amber-800'
                        }`}
                        title={`${count} counter example${count === 1 ? '' : 's'} in library`}
                      >
                        {count}
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
