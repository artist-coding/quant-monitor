import { useEffect, useRef } from 'react';
import { NavLink } from 'react-router-dom';
import { NAV_ITEMS } from '../../lib/constants';
import { useAppStore } from '../../stores/appStore';
import { useResponsiveSidebar } from '../../lib/hooks';
import Icon, { type IconName } from '../ui/Icon';

const navIcons: Record<string, IconName> = {
  '/': 'dashboard',
  '/screen': 'target',
  '/watchlist': 'star',
  '/backtest': 'backtest',
  '/simulator': 'simulator',
  '/trades': 'trade',
  '/settings': 'settings',
};

export default function Sidebar() {
  const collapsed = useAppStore((s) => s.sidebarCollapsed);
  const setSidebarCollapsed = useAppStore((s) => s.setSidebarCollapsed);
  const initialAutoApplied = useRef(false);

  useResponsiveSidebar(768);
  useEffect(() => {
    const onNarrow = () => {
      if (!initialAutoApplied.current && window.innerWidth < 768) {
        setSidebarCollapsed(true);
        initialAutoApplied.current = true;
      } else if (window.innerWidth >= 768) {
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
      className={`relative z-50 flex shrink-0 flex-col overflow-hidden border-r border-border/60 bg-[#090d0b]/92 backdrop-blur-2xl transition-[width] duration-300 ease-[cubic-bezier(0.2,0.8,0.2,1)] ${
        collapsed ? 'w-[72px]' : 'w-[236px]'
      }`}
    >
      <div className="flex h-[72px] shrink-0 items-center gap-3 border-b border-border/50 px-[18px]">
        <div className="relative flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-xl border border-accent-gold/25 bg-accent-gold/[0.07] text-sm font-black text-accent-gold shadow-[0_0_24px_rgba(201,255,99,0.08)]">
          <span className="relative z-10">Z</span>
          <span className="absolute -bottom-3 -right-3 h-6 w-6 rounded-full bg-accent-gold/20 blur-md" />
        </div>
        {!collapsed && (
          <div className="min-w-0 whitespace-nowrap">
            <div className="text-[13px] font-extrabold tracking-[0.18em] text-text-primary">ZETTA</div>
            <div className="font-mono text-[8px] tracking-[0.22em] text-text-muted">QUANT SYSTEM</div>
          </div>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto px-3 py-6">
        {!collapsed && <div className="micro-label mb-3 px-3 text-text-muted/70">Workspace</div>}
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.path}
            to={item.path}
            end={item.path === '/'}
            title={collapsed ? item.label : undefined}
            className={({ isActive }) =>
              `group relative mb-1 flex h-11 items-center gap-3 rounded-xl px-3 text-sm transition-all duration-200 whitespace-nowrap ${
                isActive
                  ? 'bg-accent-gold/[0.09] text-accent-gold shadow-[inset_0_0_0_1px_rgba(201,255,99,0.08)]'
                  : 'text-text-secondary hover:bg-white/[0.035] hover:text-text-primary'
              }`
            }
          >
            {({ isActive }) => (
              <>
                {isActive && <span className="absolute left-0 h-5 w-[2px] rounded-r-full bg-accent-gold shadow-[0_0_10px_rgba(201,255,99,0.6)]" />}
                <Icon name={navIcons[item.path] ?? 'dashboard'} size={18} className="shrink-0" />
                {!collapsed && <span className="truncate font-medium">{item.label}</span>}
                {!collapsed && isActive && <span className="ml-auto h-1 w-1 rounded-full bg-accent-gold" />}
              </>
            )}
          </NavLink>
        ))}
      </nav>

      <div className="shrink-0 border-t border-border/50 p-3">
        <div className={`rounded-xl border border-border/50 bg-white/[0.018] ${collapsed ? 'flex h-11 items-center justify-center' : 'p-3'}`}>
          {collapsed ? (
            <span className="live-dot h-2 w-2 rounded-full bg-accent-green" title="系统在线" />
          ) : (
            <div className="flex items-center gap-2.5">
              <span className="live-dot h-2 w-2 rounded-full bg-accent-green" />
              <div className="min-w-0">
                <div className="text-[11px] font-semibold text-text-secondary">SYSTEM ONLINE</div>
                <div className="font-mono text-[9px] text-text-muted">CORE v3.6.0</div>
              </div>
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}
