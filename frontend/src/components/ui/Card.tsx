import type { ReactNode } from 'react';

interface CardProps {
  title?: string;
  children: ReactNode;
  className?: string;
  /** highlight 模式：金色边框强调（用于 Hero 卡片已自带样式，此处备用） */
  highlight?: boolean;
  /** 内容区是否去掉默认内边距（用于表格等贴边内容） */
  noPadding?: boolean;
}

export default function Card({ title, children, className = '', highlight = false, noPadding = false }: CardProps) {
  const baseClasses = 'relative rounded-xl border bg-gradient-to-b from-bg-card to-[rgba(13,18,32,0.45)] backdrop-blur-xl transition-all duration-300 shadow-[0_1px_2px_rgba(0,0,0,0.25)]';
  const normalClasses = 'border-border/60 hover:border-border';
  const highlightClasses = 'border-accent-gold/30 from-accent-gold/[0.07] to-[rgba(13,18,32,0.45)] shadow-[0_0_36px_-16px_rgba(245,185,66,0.35)] hover:border-accent-gold/45';

  return (
    <div className={`${baseClasses} ${highlight ? highlightClasses : normalClasses} ${className}`}>
      {title && (
        <div className={`flex items-center gap-2.5 border-b border-border/40 px-5 py-3 ${highlight ? 'bg-gradient-to-r from-accent-gold/[0.06] to-transparent' : ''}`}>
          <span className={`h-3.5 w-1 rounded-full ${highlight ? 'bg-accent-gold' : 'bg-gradient-to-b from-accent-gold/80 to-accent-gold/30'}`} />
          <h3 className="text-[13px] font-semibold text-text-primary tracking-wide">{title}</h3>
        </div>
      )}
      <div className={noPadding ? '' : 'p-5'}>{children}</div>
    </div>
  );
}
