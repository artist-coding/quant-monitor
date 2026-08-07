import { useState, useRef, useMemo, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { useAppStore } from '../../stores/appStore';
import { useGlobalShortcuts } from '../../lib/hooks';
import { NAV_ITEMS } from '../../lib/constants';
import { IconSearch, IconPanelOpen, IconPanelClose } from '../ui/icons';

function useClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

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
  const now = useClock();

  const isDashboard = location.pathname === '/';
  const currentNav = useMemo(() => {
    const item = NAV_ITEMS.find((n) => (n.path === '/' ? location.pathname === '/' : location.pathname.startsWith(n.path)));
    if (location.pathname.startsWith('/stock/')) return '个股分析';
    if (location.pathname === '/research') return '分析记录';
    if (location.pathname.startsWith('/research/')) return '股票分析';
    return item?.label ?? '';
  }, [location.pathname]);

  // ⌘K / Ctrl+K 聚焦搜索框
  useGlobalShortcuts(
    useMemo(
      () => [
        {
          key: 'k',
          meta: true,
          handler: () => inputRef.current?.focus(),
        },
        {
          key: 'k',
          ctrl: true,
          handler: () => inputRef.current?.focus(),
        },
      ],
      [],
    ),
  );

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    const code = input.trim();
    if (!code) return;
    // 自动补全后缀
    let tsCode = code.toUpperCase();
    if (/^\d{6}$/.test(tsCode)) {
      tsCode = tsCode.startsWith('6') ? `${tsCode}.SH` : `${tsCode}.SZ`;
    }
    addSearchHistory(tsCode);
    navigate(`/stock/${tsCode}`);
    setInput('');
    setHistoryOpen(false);
  };

  const timeStr = now.toLocaleTimeString('zh-CN', { hour12: false });
  const dateStr = now.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit', weekday: 'short' });

  return (
    <header className="flex h-14 items-center justify-between gap-4 border-b border-border/60 bg-bg-secondary/60 backdrop-blur-2xl px-4 z-40 sticky top-0">
      <div className="flex items-center gap-3 min-w-0">
        <button
          onClick={toggleSidebar}
          aria-label={sidebarCollapsed ? '展开侧栏' : '收起侧栏'}
          title={sidebarCollapsed ? '展开侧栏' : '收起侧栏'}
          className="flex h-8 w-8 items-center justify-center rounded-lg text-text-muted hover:text-text-primary hover:bg-bg-hover/70 transition-colors"
        >
          {sidebarCollapsed ? <IconPanelOpen size={17} /> : <IconPanelClose size={17} />}
        </button>

        {currentNav && (
          <div className="hidden sm:flex items-center gap-2 text-sm">
            <span className="text-text-muted/60 font-light">/</span>
            <span className="font-semibold text-text-primary">{currentNav}</span>
          </div>
        )}

        {/* 全局搜索框:Dashboard 页用 Hero 搜索框,这里隐藏避免重复 */}
        {!isDashboard && (
          <form onSubmit={handleSearch} className="flex items-center relative ml-2">
            <IconSearch size={15} className="absolute left-3 text-text-muted pointer-events-none" />
            <input
              ref={inputRef}
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onFocus={() => setHistoryOpen(true)}
              onBlur={() => setTimeout(() => setHistoryOpen(false), 150)}
              placeholder="输入股票代码，如 600487.SH"
              className="input-dark w-64 md:w-72 !pl-9 !pr-14 !py-1.5 !rounded-full"
            />
            <kbd className="absolute right-3 hidden md:inline-flex items-center rounded border border-border/60 bg-bg-primary/80 px-1.5 py-0.5 text-[10px] text-text-muted font-mono pointer-events-none">⌘K</kbd>
            {historyOpen && searchHistory.length > 0 && (
              <div className="absolute top-full mt-2 left-0 w-64 md:w-72 rounded-xl border border-border/70 bg-bg-elevated shadow-2xl shadow-black/50 backdrop-blur-xl z-50 overflow-hidden">
                <div className="px-3 py-2 text-[11px] text-text-muted border-b border-border/40 flex items-center justify-between">
                  <span>最近查询</span>
                  <button
                    type="button"
                    onMouseDown={(e) => { e.preventDefault(); useAppStore.getState().clearSearchHistory(); }}
                    className="text-text-muted hover:text-accent-red transition-colors"
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
                      setInput(code);
                      navigate(`/stock/${code}`);
                      setInput('');
                      setHistoryOpen(false);
                    }}
                    className="w-full text-left px-3 py-2 text-sm font-mono text-text-secondary hover:bg-bg-hover/70 hover:text-accent-gold transition-colors"
                  >
                    {code}
                  </button>
                ))}
              </div>
            )}
          </form>
        )}
      </div>

      <div className="flex items-center gap-3 shrink-0">
        <div className="hidden md:flex flex-col items-end leading-none">
          <span className="text-sm font-mono font-semibold text-text-primary tabular-nums">{timeStr}</span>
          <span className="text-[10px] text-text-muted mt-1">{dateStr}</span>
        </div>
        <span className="hidden md:block h-6 w-px bg-border/60" />
        <span className="flex items-center gap-1.5 text-[11px] text-text-muted">
          <span className="h-1.5 w-1.5 rounded-full bg-accent-green animate-soft-pulse" />
          中国人做多中国
        </span>
      </div>
    </header>
  );
}
