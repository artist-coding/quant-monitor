import { useMemo, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { fetchWatchlist, scanWatchlist } from '../api/watchlist';
import Button from '../components/ui/Button';
import Badge from '../components/ui/Badge';
import Icon, { type IconName } from '../components/ui/Icon';

const quickActions: Array<{ icon: IconName; title: string; label: string; path: string; tone: string }> = [
  { icon: 'target', title: '策略选股', label: '12+ 战法信号交叉筛选', path: '/screen', tone: 'text-accent-gold bg-accent-gold/[0.08] border-accent-gold/15' },
  { icon: 'backtest', title: '回测实验室', label: '验证收益、回撤与胜率', path: '/backtest', tone: 'text-accent-blue bg-accent-blue/[0.08] border-accent-blue/15' },
  { icon: 'simulator', title: '交易模拟器', label: '真实约束下推演执行', path: '/simulator', tone: 'text-accent-purple bg-accent-purple/[0.08] border-accent-purple/15' },
];

const metricMeta: Array<{ key: 'watchlist' | 'b1' | 'risk' | 'abnormal'; label: string; icon: IconName; hint: string; tone: string }> = [
  { key: 'watchlist', label: '观察池', icon: 'eye', hint: '持续追踪标的', tone: 'text-accent-gold' },
  { key: 'b1', label: 'B1 信号', icon: 'target', hint: '待验证机会', tone: 'text-accent-green' },
  { key: 'risk', label: '风险预警', icon: 'shield', hint: '严格执行纪律', tone: 'text-accent-red' },
  { key: 'abnormal', label: '异动事件', icon: 'activity', hint: '量价变化监测', tone: 'text-accent-blue' },
];

export default function Dashboard() {
  const navigate = useNavigate();
  const [searchCode, setSearchCode] = useState('');

  const { data: watchlist, isError: watchlistOffline } = useQuery({
    queryKey: ['watchlist'],
    queryFn: fetchWatchlist,
  });

  const { data: scanResult, isLoading: scanning, refetch: doScan } = useQuery({
    queryKey: ['dashboard-scan'],
    queryFn: scanWatchlist,
    enabled: false,
  });

  const today = useMemo(
    () => new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }).format(new Date()),
    [],
  );

  const marketSession = useMemo(() => {
    const now = new Date();
    const day = now.getDay();
    const minutes = now.getHours() * 60 + now.getMinutes();
    const trading = day > 0 && day < 6 && ((minutes >= 570 && minutes <= 690) || (minutes >= 780 && minutes <= 900));
    return trading ? '交易时段' : '非交易时段';
  }, []);

  const metrics = {
    watchlist: watchlist?.count ?? 0,
    b1: scanResult?.b1_count ?? 0,
    risk: scanResult?.exit_count ?? 0,
    abnormal: scanResult?.abnormal_count ?? 0,
  };

  const signals = [
    { label: 'B1 买点', value: scanResult?.b1_count ?? 0, color: 'bg-accent-green' },
    { label: 'B2 确认', value: scanResult?.b2_count ?? 0, color: 'bg-accent-blue' },
    { label: '突破', value: scanResult?.break_count ?? 0, color: 'bg-accent-gold' },
    { label: '异动', value: scanResult?.abnormal_count ?? 0, color: 'bg-accent-purple' },
    { label: '风险', value: scanResult?.exit_count ?? 0, color: 'bg-accent-red' },
  ];
  const maxSignal = Math.max(1, ...signals.map((signal) => signal.value));

  const handleSearch = (e: FormEvent) => {
    e.preventDefault();
    const code = searchCode.trim().toUpperCase();
    if (!code) return;
    let tsCode = code;
    if (/^\d{6}$/.test(tsCode)) {
      tsCode = tsCode.startsWith('6') ? `${tsCode}.SH` : `${tsCode}.SZ`;
    }
    navigate(`/stock/${tsCode}`);
  };

  return (
    <div className="space-y-5 pb-4 md:space-y-6">
      <section className="flex flex-col justify-between gap-4 md:flex-row md:items-end">
        <div>
          <div className="mb-2 flex items-center gap-2">
            <span className="micro-label text-accent-gold">Market Command Center</span>
            <span className="h-px w-8 bg-accent-gold/30" />
            <span className="font-mono text-[9px] text-text-muted">{today}</span>
          </div>
          <h1 className="text-2xl font-semibold tracking-[-0.04em] text-text-primary md:text-[32px]">
            先看风险，<span className="text-text-muted">再找机会。</span>
          </h1>
          <p className="mt-2 text-xs text-text-muted md:text-sm">把复杂市场压缩成可执行的信号与纪律。</p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-2 rounded-xl border border-border/60 bg-white/[0.025] px-3 py-2">
            <span className={`h-1.5 w-1.5 rounded-full ${marketSession === '交易时段' ? 'live-dot bg-accent-green' : 'bg-text-muted'}`} />
            <span className="font-mono text-[9px] tracking-wider text-text-secondary">A-SHARE · {marketSession}</span>
          </div>
          <div className={`rounded-xl border px-3 py-2 font-mono text-[9px] tracking-wider ${watchlistOffline ? 'border-accent-red/15 bg-accent-red/[0.04] text-accent-red' : 'border-accent-green/15 bg-accent-green/[0.04] text-accent-green'}`}>
            {watchlistOffline ? 'API OFFLINE' : 'DATA READY'}
          </div>
        </div>
      </section>

      <section className="glass-panel panel-highlight scan-sheen relative overflow-hidden rounded-[24px] p-5 md:p-7 xl:p-8">
        <div className="pointer-events-none absolute inset-y-0 right-0 hidden w-[44%] lg:block">
          <div className="absolute inset-0 bg-[radial-gradient(circle_at_60%_45%,rgba(201,255,99,0.10),transparent_42%)]" />
          <svg className="absolute inset-x-0 bottom-0 h-[90%] w-full opacity-65" viewBox="0 0 640 260" preserveAspectRatio="none" aria-hidden="true">
            <defs>
              <linearGradient id="signal-line" x1="0" x2="1">
                <stop offset="0" stopColor="#67e8f9" stopOpacity="0" />
                <stop offset="0.55" stopColor="#67e8f9" stopOpacity="0.85" />
                <stop offset="1" stopColor="#c9ff63" stopOpacity="0.9" />
              </linearGradient>
              <linearGradient id="signal-fill" x1="0" x2="0" y1="0" y2="1">
                <stop stopColor="#c9ff63" stopOpacity="0.12" />
                <stop offset="1" stopColor="#c9ff63" stopOpacity="0" />
              </linearGradient>
            </defs>
            <path d="M0 218 C55 212 72 195 112 202 S173 174 210 184 S265 140 305 153 S370 116 403 126 S452 83 489 102 S552 56 640 43 L640 260 L0 260Z" fill="url(#signal-fill)" />
            <path d="M0 218 C55 212 72 195 112 202 S173 174 210 184 S265 140 305 153 S370 116 403 126 S452 83 489 102 S552 56 640 43" fill="none" stroke="url(#signal-line)" strokeWidth="2" />
            <g fill="#c9ff63">
              <circle cx="210" cy="184" r="3" /><circle cx="403" cy="126" r="3" /><circle cx="489" cy="102" r="3" /><circle cx="640" cy="43" r="4" />
            </g>
          </svg>
          <div className="absolute right-7 top-7 font-mono text-[9px] tracking-[0.18em] text-text-muted/70">SIGNAL FLOW / 01</div>
        </div>

        <div className="relative z-10 max-w-2xl">
          <div className="mb-5 flex items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-xl border border-accent-gold/15 bg-accent-gold/[0.07] text-accent-gold"><Icon name="sparkles" size={15} /></span>
            <div>
              <div className="micro-label text-text-muted">Deep Analysis</div>
              <div className="mt-0.5 text-xs font-medium text-text-secondary">输入代码，打开个股决策仪表盘</div>
            </div>
          </div>
          <form onSubmit={handleSearch} className="flex flex-col gap-2 sm:flex-row">
            <label className="relative flex-1">
              <Icon name="search" size={18} className="absolute left-4 top-1/2 -translate-y-1/2 text-text-muted" />
              <input
                type="text"
                value={searchCode}
                onChange={(e) => setSearchCode(e.target.value)}
                placeholder="输入股票代码，例如 600487"
                aria-label="股票代码"
                className="h-14 w-full rounded-2xl border border-border/70 bg-black/30 pl-12 pr-4 text-sm text-text-primary outline-none transition-all placeholder:text-text-muted/70 focus:border-accent-gold/35 focus:bg-black/40 focus:ring-4 focus:ring-accent-gold/[0.045]"
              />
            </label>
            <button type="submit" className="group flex h-14 items-center justify-center gap-3 rounded-2xl bg-accent-gold px-6 text-sm font-bold text-[#0a0d0b] shadow-[0_12px_32px_rgba(201,255,99,0.13)] transition-all hover:bg-[#d7ff89] active:scale-[0.98]">
              开始分析
              <Icon name="arrow-right" size={17} className="transition-transform group-hover:translate-x-0.5" />
            </button>
          </form>
          <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 font-mono text-[9px] tracking-wider text-text-muted">
            <span className="flex items-center gap-1.5"><Icon name="check" size={12} className="text-accent-green" /> 60+ 技术指标</span>
            <span className="flex items-center gap-1.5"><Icon name="check" size={12} className="text-accent-green" /> 多战法共振</span>
            <span className="flex items-center gap-1.5"><Icon name="check" size={12} className="text-accent-green" /> 风险边界检查</span>
          </div>
        </div>
      </section>

      <section className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        {metricMeta.map((item) => (
          <div key={item.key} className="glass-panel group rounded-2xl p-4 transition-all duration-300 hover:-translate-y-0.5 hover:border-white/15 md:p-5">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-[11px] font-medium text-text-muted">{item.label}</div>
                <div className={`number-display mt-2 text-2xl font-semibold md:text-[30px] ${item.tone}`}>{metrics[item.key].toString().padStart(2, '0')}</div>
              </div>
              <span className={`flex h-9 w-9 items-center justify-center rounded-xl border border-current/10 bg-current/[0.045] ${item.tone}`}>
                <Icon name={item.icon} size={16} />
              </span>
            </div>
            <div className="mt-3 flex items-center gap-2 border-t border-border/35 pt-3">
              <span className="h-1 w-1 rounded-full bg-text-muted" />
              <span className="text-[10px] text-text-muted">{item.hint}</span>
            </div>
          </div>
        ))}
      </section>

      <section className="grid gap-5 xl:grid-cols-12">
        <div className="glass-panel rounded-[22px] p-5 md:p-6 xl:col-span-7">
          <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-center">
            <div>
              <div className="micro-label text-text-muted">Signal Intelligence</div>
              <h2 className="mt-2 text-lg font-semibold text-text-primary">今日信号分布</h2>
            </div>
            <Button size="md" onClick={() => doScan()} disabled={scanning}>
              <Icon name={scanning ? 'refresh' : 'scan'} size={14} className={scanning ? 'animate-spin' : ''} />
              {scanning ? '扫描进行中' : '扫描观察池'}
            </Button>
          </div>

          <div className="mt-6 grid gap-6 sm:grid-cols-[160px_1fr] md:grid-cols-[190px_1fr]">
            <div className="relative flex aspect-square items-center justify-center rounded-full border border-border/50 bg-[radial-gradient(circle,rgba(201,255,99,0.07),transparent_64%)]">
              <div className="absolute inset-3 rounded-full border border-dashed border-accent-gold/18" />
              <div className="absolute inset-7 rounded-full border border-accent-gold/10" />
              <div className="relative text-center">
                <div className="number-display text-4xl font-semibold text-text-primary">{scanResult?.total ?? 0}</div>
                <div className="micro-label mt-1 text-text-muted">Scanned</div>
              </div>
              <span className="absolute right-[12%] top-[24%] h-2 w-2 rounded-full bg-accent-gold shadow-[0_0_12px_rgba(201,255,99,0.7)]" />
              <span className="absolute bottom-[18%] left-[18%] h-1.5 w-1.5 rounded-full bg-accent-blue shadow-[0_0_10px_rgba(103,232,249,0.6)]" />
            </div>

            <div className="flex flex-col justify-center space-y-4">
              {signals.map((signal) => (
                <div key={signal.label}>
                  <div className="mb-1.5 flex items-center justify-between text-[10px]">
                    <span className="text-text-secondary">{signal.label}</span>
                    <span className="font-mono text-text-primary">{signal.value.toString().padStart(2, '0')}</span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.045]">
                    <div className={`h-full rounded-full ${signal.color} transition-all duration-700`} style={{ width: `${scanResult ? Math.max(6, (signal.value / maxSignal) * 100) : 0}%` }} />
                  </div>
                </div>
              ))}
              {!scanResult && <p className="pt-1 text-[10px] leading-5 text-text-muted">执行扫描后，这里会展示观察池中的买点、突破、异动与风险信号结构。</p>}
            </div>
          </div>
        </div>

        <div className="glass-panel rounded-[22px] p-5 md:p-6 xl:col-span-5">
          <div className="flex items-center justify-between">
            <div>
              <div className="micro-label text-text-muted">Observation Pool</div>
              <h2 className="mt-2 text-lg font-semibold text-text-primary">重点观察</h2>
            </div>
            <button onClick={() => navigate('/watchlist')} className="flex h-9 w-9 items-center justify-center rounded-xl border border-border/60 text-text-muted transition-colors hover:border-accent-gold/20 hover:text-accent-gold" aria-label="查看全部自选股">
              <Icon name="arrow-right" size={15} />
            </button>
          </div>

          <div className="mt-5 space-y-2">
            {watchlist?.items.slice(0, 4).map((item, index) => (
              <button key={item.ts_code} onClick={() => navigate(`/stock/${item.ts_code}`)} className="group flex w-full items-center gap-3 rounded-xl border border-transparent px-2 py-2.5 text-left transition-all hover:border-border/50 hover:bg-white/[0.025]">
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-white/[0.04] font-mono text-[10px] text-text-muted">{String(index + 1).padStart(2, '0')}</span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-xs font-medium text-text-primary">{item.name || item.ts_code}</span>
                  <span className="mt-0.5 block truncate font-mono text-[9px] text-text-muted">{item.ts_code}</span>
                </span>
                {item.tags && <span className="hidden max-w-20 truncate rounded-full bg-white/[0.04] px-2 py-1 text-[9px] text-text-muted sm:block">{item.tags}</span>}
                <Icon name="chevron-right" size={14} className="text-text-muted transition-transform group-hover:translate-x-0.5 group-hover:text-accent-gold" />
              </button>
            ))}

            {!watchlist?.items.length && (
              <div className="flex min-h-[208px] flex-col items-center justify-center rounded-2xl border border-dashed border-border/60 bg-black/10 px-6 text-center">
                <span className="flex h-11 w-11 items-center justify-center rounded-2xl border border-accent-gold/15 bg-accent-gold/[0.05] text-accent-gold"><Icon name="star" size={18} /></span>
                <div className="mt-3 text-xs font-medium text-text-secondary">观察池还是空的</div>
                <div className="mt-1 text-[10px] leading-5 text-text-muted">添加关注标的，系统将持续追踪关键信号。</div>
                <Button variant="secondary" size="sm" className="mt-4" onClick={() => navigate('/watchlist')}>
                  <Icon name="plus" size={12} /> 添加标的
                </Button>
              </div>
            )}
          </div>
        </div>
      </section>

      {scanResult && scanResult.alerts.length > 0 && (
        <section className="glass-panel rounded-[22px] p-5 md:p-6">
          <div className="mb-4 flex items-center justify-between">
            <div>
              <div className="micro-label text-text-muted">Action Required</div>
              <h2 className="mt-2 text-lg font-semibold text-text-primary">需要关注的信号</h2>
            </div>
            <span className="font-mono text-[10px] text-text-muted">{scanResult.alerts.length} EVENTS</span>
          </div>
          <div className="divide-y divide-border/35">
            {scanResult.alerts.slice(0, 6).map((alert, index) => (
              <button key={`${alert.ts_code}-${index}`} onClick={() => navigate(`/stock/${alert.ts_code}`)} className="group flex w-full items-center gap-3 py-3.5 text-left">
                <Badge variant={alert.level === 'CRITICAL' ? 'danger' : alert.level === 'WARNING' ? 'warning' : 'info'}>{alert.level}</Badge>
                <span className="w-24 shrink-0 font-mono text-[10px] text-accent-gold">{alert.ts_code}</span>
                <span className="hidden w-24 shrink-0 text-xs text-text-secondary sm:block">{alert.alert_type}</span>
                <span className="min-w-0 flex-1 truncate text-[11px] text-text-muted">{alert.message}</span>
                <Icon name="chevron-right" size={14} className="text-text-muted group-hover:text-accent-gold" />
              </button>
            ))}
          </div>
        </section>
      )}

      <section>
        <div className="mb-3 flex items-center justify-between">
          <div className="micro-label text-text-muted">Quick Operations</div>
          <span className="font-mono text-[9px] text-text-muted/70">03 MODULES</span>
        </div>
        <div className="grid gap-3 md:grid-cols-3">
          {quickActions.map((item) => (
            <button key={item.path} onClick={() => navigate(item.path)} className="glass-panel group flex items-center gap-4 rounded-2xl p-4 text-left transition-all duration-300 hover:-translate-y-0.5 hover:border-white/15 md:p-5">
              <span className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl border ${item.tone}`}><Icon name={item.icon} size={18} /></span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-semibold text-text-primary">{item.title}</span>
                <span className="mt-1 block truncate text-[10px] text-text-muted">{item.label}</span>
              </span>
              <Icon name="arrow-right" size={15} className="text-text-muted transition-all group-hover:translate-x-1 group-hover:text-accent-gold" />
            </button>
          ))}
        </div>
      </section>

      <footer className="flex flex-col justify-between gap-2 border-t border-border/35 pt-4 font-mono text-[8px] tracking-wider text-text-muted/60 sm:flex-row sm:items-center">
        <span>ZETTA QUANT SYSTEM · STRUCTURED DECISION ENGINE</span>
        <span>数据仅供研究，不构成任何投资建议</span>
      </footer>
    </div>
  );
}
