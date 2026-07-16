import { useState, useRef, useMemo } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { useAppStore } from '../../stores/appStore';
import { useGlobalShortcuts } from '../../lib/hooks';
import Icon from '../ui/Icon';

const pageMeta: Record<string, { eyebrow: string; title: string }> = {
  '/': { eyebrow: 'Command Center', title: '市场总览' },
  '/screen': { eyebrow: 'Signal Engine', title: '策略选股' },
  '/watchlist': { eyebrow: 'Observation Pool', title: '自选观察' },
  '/backtest': { eyebrow: 'Research Lab', title: '策略回测' },
  '/simulator': { eyebrow: 'Execution Lab', title: '交易模拟' },
  '/trades': { eyebrow: 'Trade Ledger', title: '交易记录' },
  '/settings': { eyebrow: 'System Control', title: '系统设置' },
};

export default function Header() {
  const navigate = useNavigate();
  const location = useLocation();
  const toggleSidebar = useAppStore((s) => s.toggleSidebar);
  const sidebarCollapsed = useAppStore((s) => s.sidebarCollapsed);
  const searchHistory = useAppStore((s) => s.searchHistory);
  const addSearchHistory = useAppStore((s) => s.addSearchHistory);
  const [input, setInput] = useState('');
  const [historyOpen, setHistoryOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const meta = location.pathname.startsWith('/stock/')
    ? { eyebrow: 'Deep Analysis', title: '个股分析' }
    : pageMeta[location.pathname] ?? pageMeta['/'];

  useGlobalShortcuts(
    useMemo(
      () => [
        { key: 'k', meta: true, handler: () => inputRef.current?.focus() },
        { key: 'k', ctrl: true, handler: () => inputRef.current?.focus() },
      ],
      [],
    ),
  );

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    const code = input.trim();
    if (!code) return;
    let tsCode = code.toUpperCase();
    if (/^\d{6}$/.test(tsCode)) {
      tsCode = tsCode.startsWith('6') ? `${tsCode}.SH` : `${tsCode}.SZ`;
    }
    addSearchHistory(tsCode);
    navigate(`/stock/${tsCode}`);
    setInput('');
    setHistoryOpen(false);
  };

  return (
    <header className="sticky top-0 z-40 flex h-[72px] shrink-0 items-center justify-between border-b border-border/50 bg-[#090d0b]/72 px-4 backdrop-blur-2xl md:px-6">
      <div className="flex min-w-0 items-center gap-3 md:gap-4">
        <button
          onClick={toggleSidebar}
          aria-label={sidebarCollapsed ? '展开侧栏' : '收起侧栏'}
          title={sidebarCollapsed ? '展开侧栏' : '收起侧栏'}
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-border/60 bg-white/[0.025] text-text-muted transition-all hover:border-accent-gold/25 hover:text-accent-gold"
        >
          <Icon name="menu" size={17} className={`transition-transform ${sidebarCollapsed ? '' : 'rotate-90'}`} />
        </button>
        <div className="hidden min-w-[126px] sm:block">
          <div className="micro-label truncate text-text-muted">{meta.eyebrow}</div>
          <div className="mt-0.5 truncate text-sm font-semibold text-text-primary">{meta.title}</div>
        </div>
        <div className="hidden h-7 w-px bg-border/60 lg:block" />
        <form onSubmit={handleSearch} className="relative hidden items-center lg:flex">
          <Icon name="search" size={15} className="pointer-events-none absolute left-3 text-text-muted" />
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onFocus={() => setHistoryOpen(true)}
            onBlur={() => setTimeout(() => setHistoryOpen(false), 150)}
            placeholder="搜索股票代码 / 名称"
            className="h-9 w-64 rounded-xl border border-border/60 bg-black/20 pl-9 pr-12 text-xs text-text-primary outline-none transition-all placeholder:text-text-muted/75 focus:w-72 focus:border-accent-gold/35 focus:bg-black/30 focus:ring-4 focus:ring-accent-gold/[0.04]"
          />
          {historyOpen && searchHistory.length > 0 && (
            <div className="absolute left-0 top-full z-50 mt-2 w-72 overflow-hidden rounded-xl border border-border bg-[#101713]/95 shadow-2xl backdrop-blur-2xl">
              <div className="flex items-center justify-between border-b border-border/40 px-3 py-2 text-xs text-text-muted">
                <span>最近查询</span>
                <button
                  type="button"
                  onMouseDown={(e) => { e.preventDefault(); useAppStore.getState().clearSearchHistory(); }}
                  className="transition-colors hover:text-accent-red"
                >
                  清除
                </button>
              </div>
              {searchHistory.slice(0, 6).map((code) => (
                <button
                  key={code}
                  type="button"
                  onMouseDown={(e) => {
                    e.preventDefault();
                    navigate(`/stock/${code}`);
                    setInput('');
                    setHistoryOpen(false);
                  }}
                  className="w-full px-3 py-2 text-left font-mono text-xs text-text-secondary transition-colors hover:bg-bg-hover hover:text-accent-gold"
                >
                  {code}
                </button>
              ))}
            </div>
          )}
          <kbd className="pointer-events-none absolute right-2 rounded-md border border-border/70 bg-white/[0.03] px-1.5 py-0.5 font-mono text-[9px] text-text-muted">⌘K</kbd>
        </form>
      </div>

      <div className="flex items-center gap-2 md:gap-3">
        <div className="hidden items-center gap-2 rounded-full border border-accent-green/15 bg-accent-green/[0.045] px-3 py-1.5 md:flex">
          <span className="live-dot h-1.5 w-1.5 rounded-full bg-accent-green" />
          <span className="font-mono text-[9px] font-semibold tracking-wider text-accent-green">LIVE DATA</span>
        </div>
        <button className="relative flex h-9 w-9 items-center justify-center rounded-xl border border-border/60 bg-white/[0.025] text-text-muted transition-colors hover:text-text-primary" aria-label="通知">
          <Icon name="bell" size={16} />
          <span className="absolute right-2 top-2 h-1.5 w-1.5 rounded-full bg-accent-red ring-2 ring-[#0b100e]" />
        </button>
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-accent-gold to-accent-cyan text-xs font-black text-[#0a0d0b] shadow-[0_0_18px_rgba(201,255,99,0.12)]">ZG</div>
      </div>
    </header>
  );
}
