import type { ReactNode } from 'react';

type Tone = 'gray' | 'blue' | 'green' | 'amber' | 'red' | 'purple' | 'sky' | 'orange' | 'fuchsia';

const TONE_CLASSES: Record<Tone, string> = {
  gray: 'bg-gray-100 text-gray-700 border-gray-200',
  blue: 'bg-blue-50 text-blue-700 border-blue-200',
  green: 'bg-green-50 text-green-700 border-green-200',
  amber: 'bg-amber-50 text-amber-800 border-amber-200',
  red: 'bg-red-50 text-red-700 border-red-200',
  purple: 'bg-purple-50 text-purple-700 border-purple-200',
  sky: 'bg-sky-50 text-sky-700 border-sky-200',
  orange: 'bg-orange-50 text-orange-700 border-orange-200',
  fuchsia: 'bg-fuchsia-50 text-fuchsia-700 border-fuchsia-200',
};

interface BadgeProps {
  tone?: Tone;
  icon?: ReactNode;
  children: ReactNode;
  className?: string;
  title?: string;
}

export function Badge({ tone = 'gray', icon, children, className = '', title }: BadgeProps) {
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-xs font-medium ${TONE_CLASSES[tone]} ${className}`} title={title}>
      {icon}
      {children}
    </span>
  );
}
