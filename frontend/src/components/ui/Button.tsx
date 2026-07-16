import type { ButtonHTMLAttributes, ReactNode } from 'react';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  children: ReactNode;
  variant?: 'primary' | 'secondary' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
}

const variantClasses = {
  primary: 'border-accent-gold/25 bg-accent-gold text-[#0a0d0b] shadow-[0_8px_24px_rgba(201,255,99,0.1)] hover:bg-[#d5ff82]',
  secondary: 'border-border/60 bg-white/[0.04] text-text-secondary hover:border-white/15 hover:bg-white/[0.07] hover:text-text-primary',
  ghost: 'border-transparent text-text-secondary hover:bg-white/[0.045] hover:text-text-primary',
};

const sizeClasses = {
  sm: 'h-8 px-3 text-[11px]',
  md: 'h-9 px-4 text-xs',
  lg: 'h-11 px-5 text-sm',
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
      className={`inline-flex items-center justify-center gap-2 rounded-xl border font-semibold transition-all duration-200 active:scale-[0.97] focus:outline-none focus:ring-2 focus:ring-accent-gold/25 disabled:cursor-not-allowed disabled:opacity-50 ${variantClasses[variant]} ${sizeClasses[size]} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}
