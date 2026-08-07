import type { ButtonHTMLAttributes, ReactNode } from 'react';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  children: ReactNode;
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
  size?: 'sm' | 'md' | 'lg';
}

const variantClasses = {
  primary:
    'bg-gradient-to-b from-accent-gold to-[#e0a52e] text-[#231a05] font-semibold shadow-[0_2px_10px_-2px_rgba(245,185,66,0.45),inset_0_1px_0_rgba(255,255,255,0.25)] hover:brightness-110 hover:shadow-[0_4px_16px_-2px_rgba(245,185,66,0.55)] border border-accent-gold/60',
  secondary:
    'bg-bg-hover/60 text-text-secondary border border-border/70 hover:bg-bg-hover hover:text-text-primary hover:border-border',
  ghost:
    'text-text-secondary border border-transparent hover:bg-bg-hover/70 hover:text-text-primary',
  danger:
    'bg-accent-red/12 text-accent-red border border-accent-red/30 hover:bg-accent-red/20',
};

const sizeClasses = {
  sm: 'px-2.5 py-1 text-xs rounded-md gap-1',
  md: 'px-3.5 py-1.5 text-sm rounded-lg gap-1.5',
  lg: 'px-5 py-2.5 text-base rounded-lg gap-2',
};

export default function Button({
  children,
  variant = 'primary',
  size = 'md',
  className = '',
  ...props
}: ButtonProps) {
  return (
    <button
      className={`inline-flex items-center justify-center font-medium transition-all duration-200 active:scale-[0.97] focus:outline-none focus:ring-2 focus:ring-accent-gold/40 disabled:opacity-45 disabled:cursor-not-allowed disabled:pointer-events-none ${variantClasses[variant]} ${sizeClasses[size]} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}
