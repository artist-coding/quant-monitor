import type { ReactNode } from 'react';

interface CardProps {
  title?: string;
  children: ReactNode;
  className?: string;
  /** highlight 模式：金色边框强调（用于 Hero 卡片已自带样式，此处备用） */
  highlight?: boolean;
}

export default function Card({ title, children, className = '', highlight = false }: CardProps) {
  const baseClasses = 'glass-panel rounded-2xl transition-[border-color,box-shadow,transform] duration-300 hover:border-white/15 hover:shadow-2xl hover:shadow-black/20';
  const highlightClasses = 'panel-highlight border-accent-gold/25 bg-gradient-to-br from-[rgba(201,255,99,0.07)] to-bg-card shadow-[0_0_38px_-24px_rgba(201,255,99,0.4)]';

  return (
    <div className={`${baseClasses} ${highlight ? highlightClasses : ''} ${className}`}>
      {title && (
        <div className={`border-b border-border/30 px-5 py-4 ${highlight ? 'bg-gradient-to-r from-accent-gold/[0.04] to-transparent' : ''}`}>
          <h3 className="micro-label text-text-secondary">{title}</h3>
        </div>
      )}
      <div className="p-5">{children}</div>
    </div>
  );
}
