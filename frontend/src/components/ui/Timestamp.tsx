import { useEffect, useState } from 'react';

const UNITS: [string, number][] = [
  ['s', 60],
  ['m', 60],
  ['h', 24],
  ['d', Infinity],
];

function formatRelative(date: Date): string {
  let diff = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  for (const [unit, max] of UNITS) {
    if (diff < max) return `${diff}${unit} ago`;
    diff = Math.floor(diff / max);
  }
  return date.toLocaleDateString();
}

interface TimestampProps {
  date: string | Date | null | undefined;
  className?: string;
}

export function Timestamp({ date, className = '' }: TimestampProps) {
  const [, setTick] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 30000);
    return () => clearInterval(id);
  }, []);

  if (!date) return null;
  const d = typeof date === 'string' ? new Date(date) : date;
  if (isNaN(d.getTime())) return null;

  return (
    <time dateTime={d.toISOString()} title={d.toLocaleString()} className={`text-xs text-gray-500 ${className}`}>
      {formatRelative(d)}
    </time>
  );
}
