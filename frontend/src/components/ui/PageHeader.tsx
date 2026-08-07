import type { ReactNode } from 'react';

interface PageHeaderProps {
  title: string;
  description?: string;
  actions?: ReactNode;
}

/** 页头：标题 + 描述 + 右侧操作区，统一各页面入口样式 */
export default function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <div className="flex items-end justify-between gap-4 pb-1">
      <div className="min-w-0">
        <h1 className="text-xl font-bold text-text-primary tracking-tight flex items-center gap-2.5">
          <span className="inline-block w-1 h-5 rounded-full bg-gradient-to-b from-accent-gold to-accent-orange shrink-0" />
          {title}
        </h1>
        {description && (
          <p className="text-xs text-text-muted mt-1.5 ml-3.5">{description}</p>
        )}
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </div>
  );
}
