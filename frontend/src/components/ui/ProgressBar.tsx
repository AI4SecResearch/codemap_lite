interface ProgressBarProps {
  value: number; // 0-1
  label?: string;
  size?: 'sm' | 'md';
  className?: string;
}

export function ProgressBar({ value, label, size = 'sm', className = '' }: ProgressBarProps) {
  const pct = Math.min(100, Math.max(0, value * 100));
  const height = size === 'sm' ? 'h-1.5' : 'h-2.5';

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <div className={`flex-1 ${height} rounded-full bg-gray-200 overflow-hidden`}>
        <div
          className={`${height} rounded-full bg-gradient-to-r from-blue-500 to-blue-600 transition-all duration-500 ease-out`}
          style={{ width: `${pct}%` }}
        />
      </div>
      {label != null ? (
        <span className="text-xs text-gray-500 tabular-nums whitespace-nowrap">{label}</span>
      ) : (
        <span className="text-xs text-gray-500 tabular-nums">{Math.round(pct)}%</span>
      )}
    </div>
  );
}
