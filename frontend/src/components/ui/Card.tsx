import type { HTMLAttributes, ReactNode } from 'react';

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode;
  clickable?: boolean;
  active?: boolean;
}

export function Card({ children, clickable, active, className = '', ...props }: CardProps) {
  return (
    <div
      className={`bg-white border rounded-xl shadow-sm transition-all duration-200 ${
        clickable ? 'cursor-pointer hover:shadow-md hover:-translate-y-0.5' : ''
      } ${active ? 'ring-2 ring-blue-500 border-blue-300' : 'border-gray-200'} ${className}`}
      {...props}
    >
      {children}
    </div>
  );
}
