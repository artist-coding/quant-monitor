interface BadgeProps {
  children: React.ReactNode;
  variant?: 'default' | 'success' | 'warning' | 'danger' | 'info';
}

const variantClasses = {
  default: 'bg-white/[0.04] text-text-secondary border-border/60',
  success: 'bg-accent-green/[0.08] text-accent-green border-accent-green/20',
  warning: 'bg-accent-gold/[0.08] text-accent-gold border-accent-gold/20',
  danger: 'bg-accent-red/[0.08] text-accent-red border-accent-red/20',
  info: 'bg-accent-blue/[0.08] text-accent-blue border-accent-blue/20',
};

export default function Badge({ children, variant = 'default' }: BadgeProps) {
  return (
    <span className={`inline-flex items-center rounded-full border px-2.5 py-1 font-mono text-[9px] font-semibold tracking-wider ${variantClasses[variant]}`}>
      {children}
    </span>
  );
}
