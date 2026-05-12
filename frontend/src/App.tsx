import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom';
import Dashboard from './pages/Dashboard';
import SourcePointList from './pages/SourcePointList';
import FunctionBrowser from './pages/FunctionBrowser';
import CallGraphView from './pages/CallGraphView';
import ReviewQueue from './pages/ReviewQueue';
import FeedbackLog from './pages/FeedbackLog';

const navItems = [
  { path: '/', label: 'Dashboard' },
  { path: '/sources', label: 'Source Points' },
  { path: '/functions', label: 'Functions' },
  { path: '/graph', label: 'Call Graph' },
  { path: '/review', label: 'Review' },
  { path: '/feedback', label: 'Feedback' },
];

export default function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-gray-100">
        <nav className="bg-white shadow-sm border-b">
          <div className="max-w-7xl mx-auto px-4">
            <div className="flex items-center h-14 gap-6">
              <span className="font-bold text-lg">codemap-lite</span>
              {navItems.map((item) => (
                <NavLink
                  key={item.path}
                  to={item.path}
                  className={({ isActive }) =>
                    `text-sm ${isActive ? 'text-blue-600 font-medium' : 'text-gray-600 hover:text-gray-900'}`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
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
