import { Link, useLocation } from 'react-router-dom';

/**
 * Workflow stepper — guides the user through the operational pipeline:
 * 1. Sources (fetch, repair, review LLM edges + unresolved GAPs)
 * 2. Feedback (counter-example library)
 *
 * Renders as a horizontal step indicator at the top of workflow pages.
 */

const STEPS = [
  { path: '/sources', label: 'Source Points', hint: 'Fetch, repair & review' },
  { path: '/feedback', label: 'Feedback', hint: 'Counter-example library' },
] as const;

export default function WorkflowStepper() {
  const { pathname } = useLocation();

  const currentIdx = STEPS.findIndex((s) => pathname.startsWith(s.path));

  return (
    <nav className="flex items-center gap-1 px-4 py-2 bg-white border-b text-sm">
      {STEPS.map((step, i) => {
        const active = i === currentIdx;
        const done = i < currentIdx;
        return (
          <div key={step.path} className="flex items-center">
            {i > 0 && (
              <div
                className={`w-8 h-px mx-2 ${
                  done ? 'bg-blue-400' : 'bg-gray-200'
                }`}
              />
            )}
            <Link
              to={step.path}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-full transition-colors ${
                active
                  ? 'bg-blue-50 text-blue-700 ring-1 ring-blue-300'
                  : done
                  ? 'text-blue-600 hover:bg-blue-50'
                  : 'text-gray-500 hover:text-gray-700 hover:bg-gray-50'
              }`}
              title={step.hint}
            >
              <span
                className={`flex items-center justify-center w-5 h-5 rounded-full text-xs font-bold ${
                  active
                    ? 'bg-blue-600 text-white'
                    : done
                    ? 'bg-blue-100 text-blue-700'
                    : 'bg-gray-200 text-gray-500'
                }`}
              >
                {done ? '\u2713' : i + 1}
              </span>
              <span className="hidden sm:inline">{step.label}</span>
            </Link>
          </div>
        );
      })}
    </nav>
  );
}
