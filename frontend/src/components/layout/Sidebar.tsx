import { useEffect, useRef, type ReactElement } from 'react';
import { NavLink } from 'react-router-dom';
import { NAV_ITEMS } from '../../lib/constants';
import { useAppStore } from '../../stores/appStore';
import { useResponsiveSidebar } from '../../lib/hooks';
import {
  IconGrid, IconTarget, IconStar, IconHistory, IconArchive, IconActivity, IconExchange, IconSettings, LogoMark,
  IconRadar, IconBook,
} from '../ui/icons';

const NAV_ICONS: Record<string, (p: { size?: number; className?: string }) => ReactElement> = {
  grid: IconGrid,
  radar: IconRadar,
  target: IconTarget,
  star: IconStar,
  history: IconHistory,
  archive: IconArchive,
  book: IconBook,
  activity: IconActivity,
  exchange: IconExchange,
  settings: IconSettings,
};

export default function Sidebar() {
  const collapsed = useAppStore((s) => s.sidebarCollapsed);
  const setSidebarCollapsed = useAppStore((s) => s.setSidebarCollapsed);
  const initialAutoApplied = useRef(false);

  // 监听窄屏事件自动收起(< 768px),且只触发一次
  useResponsiveSidebar(768);
  useEffect(() => {
    const onNarrow = () => {
      if (!initialAutoApplied.current) {
        setSidebarCollapsed(true);
        initialAutoApplied.current = true;
      } else if (window.innerWidth >= 768) {
        // 回到宽屏:还原
        setSidebarCollapsed(false);
        initialAutoApplied.current = false;
      }
    };
    window.addEventListener('zg:narrow-screen', onNarrow);
    window.addEventListener('resize', onNarrow);
    return () => {
      window.removeEventListener('zg:narrow-screen', onNarrow);
      window.removeEventListener('resize', onNarrow);
    };
  }, [setSidebarCollapsed]);

  return (
    <aside
      className={`relative flex flex-col border-r border-border/60 bg-bg-secondary/70 backdrop-blur-2xl z-50 overflow-hidden transition-[width] duration-300 ease-[cubic-bezier(0.2,0.8,0.2,1)] ${
        collapsed ? 'w-[68px]' : 'w-60'
      }`}
    >
      {/* 侧栏内微光 */}
      <div aria-hidden className="pointer-events-none absolute -top-24 -left-24 h-56 w-56 rounded-full bg-accent-gold/[0.05] blur-3xl" />

      {/* Logo */}
      <div className={`flex h-16 items-center gap-3 border-b border-border/50 shrink-0 ${collapsed ? 'justify-center px-0' : 'px-4'}`}>
        <LogoMark size={36} className="shrink-0 drop-shadow-[0_2px_10px_rgba(245,185,66,0.35)]" />
        {!collapsed && (
          <div className="min-w-0 leading-tight">
            <div className="text-sm font-bold text-text-primary whitespace-nowrap tracking-wide">知行合一 踏踏实实</div>
            <div className="text-[9px] font-semibold tracking-[0.28em] text-text-muted uppercase mt-0.5">ZGE Quant</div>
          </div>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 px-2.5 py-3 space-y-1 overflow-y-auto">
        {NAV_ITEMS.map((item) => {
          const Icon = NAV_ICONS[item.icon] || IconGrid;
          return (
            <NavLink
              key={item.path}
              to={item.path}
              end={item.path === '/'}
              title={collapsed ? item.label : undefined}
              className={({ isActive }) =>
                `group relative flex items-center gap-3 rounded-lg px-3 py-2.5 text-[13px] font-medium transition-all duration-200 whitespace-nowrap ${
                  collapsed ? 'justify-center px-0' : ''
                } ${
                  isActive
                    ? 'bg-accent-gold/[0.12] text-accent-gold shadow-[inset_0_0_0_1px_rgba(245,185,66,0.22)]'
                    : 'text-text-secondary hover:bg-bg-hover/70 hover:text-text-primary'
                }`
              }
            >
              {({ isActive }) => (
                <>
                  {/* 激活指示条 */}
                  <span
                    className={`absolute left-0 top-1/2 -translate-y-1/2 h-5 w-[3px] rounded-r-full bg-accent-gold transition-opacity duration-200 ${
                      isActive ? 'opacity-100' : 'opacity-0'
                    }`}
                  />
                  <Icon
                    size={18}
                    className={`shrink-0 transition-transform duration-200 ${
                      isActive ? 'text-accent-gold' : 'text-text-muted group-hover:text-text-primary group-hover:scale-110'
                    }`}
                  />
                  {!collapsed && <span className="truncate">{item.label}</span>}
                </>
              )}
            </NavLink>
          );
        })}
      </nav>

      {/* Footer */}
      <div className={`border-t border-border/50 shrink-0 ${collapsed ? 'p-2 flex justify-center' : 'p-3.5'}`}>
        {collapsed ? (
          <span className="h-1.5 w-1.5 rounded-full bg-accent-green animate-soft-pulse" title="运行中" />
        ) : (
          <div className="flex items-center justify-between text-[11px] text-text-muted">
            <span className="flex items-center gap-1.5">
              <span className="h-1.5 w-1.5 rounded-full bg-accent-green animate-soft-pulse" />
              运行中
            </span>
            <span className="font-mono text-text-muted/70">v1.0.0</span>
          </div>
        )}
      </div>
    </aside>
  );
}
