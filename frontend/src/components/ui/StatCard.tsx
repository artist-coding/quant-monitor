import type { ReactNode } from 'react';

interface StatCardProps {
  label: string;
  value: ReactNode;
  icon?: ReactNode;
  /** 主题色（决定图标底色与数值颜色），默认金色 */
  tone?: 'gold' | 'red' | 'green' | 'blue' | 'purple' | 'cyan';
  /** 数值颜色覆盖（如涨跌色），传入后忽略 tone 的数值色 */
  valueClassName?: string;
  suffix?: ReactNode;
}

const toneStyles: Record<string, { tile: string; value: string }> = {
  gold: { tile: 'bg-accent-gold/12 text-accent-gold ring-accent-gold/25', value: 'text-accent-gold' },
  red: { tile: 'bg-accent-red/12 text-accent-red ring-accent-red/25', value: 'text-accent-red' },
  green: { tile: 'bg-accent-green/12 text-accent-green ring-accent-green/25', value: 'text-accent-green' },
  blue: { tile: 'bg-accent-blue/12 text-accent-blue ring-accent-blue/25', value: 'text-accent-blue' },
  purple: { tile: 'bg-accent-purple/12 text-accent-purple ring-accent-purple/25', value: 'text-accent-purple' },
  cyan: { tile: 'bg-accent-cyan/12 text-accent-cyan ring-accent-cyan/25', value: 'text-accent-cyan' },
};

/** 指标统计卡：图标 + 标签 + 大数值，用于 Dashboard / 回测 / 模拟 / 交易统计 */
export default function StatCard({ label, value, icon, tone = 'gold', valueClassName, suffix }: StatCardProps) {
  const styles = toneStyles[tone];
  return (
    <div className="card-shine group relative overflow-hidden rounded-xl border border-border/60 bg-bg-card backdrop-blur-xl px-4 py-3.5 transition-all duration-300 hover:border-border hover:bg-bg-hover/30">
      <div className="flex items-center gap-3">
        {icon && (
          <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ring-1 ring-inset ${styles.tile}`}>
            {icon}
          </div>
        )}
        <div className="min-w-0">
          <div className="text-[11px] text-text-muted font-medium tracking-wide">{label}</div>
          <div className={`text-xl font-bold tabular-nums leading-tight mt-0.5 ${valueClassName || styles.value}`}>
            {value}
            {suffix && <span className="text-xs font-medium text-text-muted ml-1">{suffix}</span>}
          </div>
        </div>
      </div>
    </div>
  );
}
