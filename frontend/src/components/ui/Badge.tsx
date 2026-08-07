interface BadgeProps {
  children: React.ReactNode;
  variant?: 'default' | 'success' | 'warning' | 'danger' | 'info';
  /** 是否显示状态点 */
  dot?: boolean;
}

const variantClasses = {
  default: 'bg-bg-hover/70 text-text-secondary ring-border',
  success: 'bg-accent-green/12 text-accent-green ring-accent-green/25',
  warning: 'bg-accent-gold/12 text-accent-gold ring-accent-gold/25',
  danger: 'bg-accent-red/12 text-accent-red ring-accent-red/25',
  info: 'bg-accent-blue/12 text-accent-blue ring-accent-blue/25',
};

const dotClasses = {
  default: 'bg-text-muted',
  success: 'bg-accent-green',
  warning: 'bg-accent-gold',
  danger: 'bg-accent-red',
  info: 'bg-accent-blue',
};

export default function Badge({ children, variant = 'default', dot = false }: BadgeProps) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium ring-1 ring-inset ${variantClasses[variant]}`}>
      {dot && <span className={`h-1 w-1 rounded-full ${dotClasses[variant]}`} />}
      {children}
    </span>
  );
}
