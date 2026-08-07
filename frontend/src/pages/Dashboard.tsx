import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { fetchWatchlist, scanWatchlist } from '../api/watchlist';
import { createResearch } from '../api/research';
import Button from '../components/ui/Button';
import Badge from '../components/ui/Badge';
import StatCard from '../components/ui/StatCard';
import {
  IconSearch, IconStar, IconZap, IconAlert, IconActivity,
  IconTarget, IconChart, IconArrowRight, IconRadar,
} from '../components/ui/icons';

export default function Dashboard() {
  const navigate = useNavigate();
  const [searchCode, setSearchCode] = useState('');

  const researchMutation = useMutation({
    mutationFn: createResearch,
    onSuccess: (task) => navigate(`/research/${task.task_id}`),
  });

  const { data: watchlist } = useQuery({
    queryKey: ['watchlist'],
    queryFn: fetchWatchlist,
  });

  const { data: scanResult, isLoading: scanning, refetch: doScan } = useQuery({
    queryKey: ['dashboard-scan'],
    queryFn: scanWatchlist,
    enabled: false,
  });

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    const code = searchCode.trim().toUpperCase();
    if (!code) return;
    let tsCode = code;
    if (/^\d{6}$/.test(tsCode)) {
      tsCode = tsCode.startsWith('6') ? `${tsCode}.SH` : `${tsCode}.SZ`;
    }
    researchMutation.mutate(tsCode);
  };

  const stats = [
    { label: '自选股', value: watchlist?.count || 0, icon: <IconStar size={18} />, tone: 'gold' as const },
    { label: 'B1 信号', value: scanResult?.b1_count || 0, icon: <IconZap size={18} />, tone: 'green' as const },
    { label: '逃顶预警', value: scanResult?.exit_count || 0, icon: <IconAlert size={18} />, tone: 'red' as const },
    { label: '异动', value: scanResult?.abnormal_count || 0, icon: <IconActivity size={18} />, tone: 'blue' as const },
  ];

  const quickActions = [
    { icon: <IconTarget size={20} />, title: '选股筛选', desc: 'B1 / B2 / B3 等 12 种战法', path: '/screen', tone: 'gold' },
    { icon: <IconStar size={20} />, title: '加入自选', desc: '把感兴趣的票放进观察池', path: '/watchlist', tone: 'blue' },
    { icon: <IconChart size={20} />, title: '策略回测', desc: '用少妇战法验证历史收益', path: '/backtest', tone: 'purple' },
  ] as const;

  const actionTone: Record<string, string> = {
    gold: 'bg-accent-gold/12 text-accent-gold ring-accent-gold/25',
    blue: 'bg-accent-blue/12 text-accent-blue ring-accent-blue/25',
    purple: 'bg-accent-purple/12 text-accent-purple ring-accent-purple/25',
  };

  return (
    <div className="space-y-7">
      {/* Hero Search */}
      <div className="relative flex flex-col items-center justify-center pt-16 pb-14 animate-fade-up">
        {/* 背景光斑 */}
        <div aria-hidden className="pointer-events-none absolute inset-0 -z-10">
          <div className="absolute left-1/2 top-0 h-72 w-[42rem] -translate-x-1/2 rounded-full bg-accent-gold/[0.09] blur-3xl" />
          <div className="absolute left-1/4 top-20 h-44 w-44 rounded-full bg-accent-blue/[0.07] blur-3xl" />
          <div className="absolute right-1/4 top-20 h-44 w-44 rounded-full bg-accent-purple/[0.06] blur-3xl" />
        </div>

        <div className="mb-6 flex items-center gap-2 rounded-full border border-accent-gold/25 bg-accent-gold/[0.08] px-4 py-1.5 text-[11px] font-medium tracking-[0.18em] text-accent-gold backdrop-blur-sm">
          <IconRadar size={13} />
          ZGE QUANT · AI 驱动的 A 股分析终端
        </div>

        <h1 className="font-display text-glow-gold text-6xl md:text-7xl font-black tracking-[0.08em] text-transparent bg-clip-text bg-gradient-to-b from-amber-100 via-accent-gold to-accent-orange">
          中国人做多中国
        </h1>
        <p className="mt-5 text-sm text-text-muted tracking-[0.5em] font-light">知是行之始，行是知之成</p>

        <div className="hero-rule mt-7 w-56" />

        <form onSubmit={handleSearch} className="group relative z-10 mt-9 w-full max-w-xl">
          <div className="absolute -inset-0.5 rounded-2xl bg-gradient-to-r from-accent-gold/40 via-accent-orange/30 to-accent-gold/40 opacity-30 blur-md transition-opacity duration-500 group-focus-within:opacity-70" />
          <div className="relative flex items-center gap-2 rounded-2xl border border-border/70 bg-bg-elevated/90 p-2 pl-4 shadow-2xl shadow-black/40 backdrop-blur-xl">
            <IconSearch size={18} className="shrink-0 text-text-muted" />
            <input
              type="text"
              value={searchCode}
              onChange={(e) => setSearchCode(e.target.value)}
              placeholder="输入股票代码或研究问题，如 A 股磷化铟公司如何"
              className="w-full bg-transparent py-2 text-[15px] text-text-primary placeholder-text-muted outline-none"
            />
            <button
              type="submit"
              disabled={researchMutation.isPending}
              className="animate-cta-glow shrink-0 rounded-xl bg-gradient-to-b from-accent-gold to-[#e0a52e] px-7 py-2.5 text-sm font-bold text-[#231a05] transition-all hover:brightness-110 active:scale-95"
            >
              {researchMutation.isPending ? '创建中...' : '分析'}
            </button>
          </div>
          {researchMutation.isError && (
            <p className="mt-2 px-2 text-xs text-accent-red">{(researchMutation.error as Error)?.message || '创建调研任务失败'}</p>
          )}
        </form>
      </div>

      {/* Quick Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 animate-fade-up animate-fade-up-1">
        {stats.map((s) => (
          <StatCard key={s.label} label={s.label} value={s.value} icon={s.icon} tone={s.tone} />
        ))}
      </div>

      {/* Watchlist Signals */}
      <div className="flex items-center justify-between animate-fade-up animate-fade-up-2">
        <h2 className="flex items-center gap-2.5 text-base font-semibold text-text-primary">
          <span className="inline-block h-4 w-1 rounded-full bg-gradient-to-b from-accent-gold to-accent-orange" />
          自选股信号
        </h2>
        <Button size="sm" variant="secondary" onClick={() => doScan()} disabled={scanning}>
          {scanning ? '扫描中...' : '扫描信号'}
        </Button>
      </div>

      {scanResult && scanResult.alerts.length > 0 && (
        <div className="space-y-2 animate-fade-up animate-fade-up-3">
          {scanResult.alerts.slice(0, 10).map((a, i) => (
            <div
              key={i}
              className="group flex items-center gap-3 rounded-xl border border-border/50 bg-bg-card/70 px-4 py-3 cursor-pointer transition-all duration-200 hover:border-accent-gold/30 hover:bg-bg-hover/40"
              onClick={() => navigate(`/stock/${a.ts_code}`)}
            >
              <Badge variant={a.level === 'CRITICAL' ? 'danger' : a.level === 'WARNING' ? 'warning' : 'info'} dot>
                {a.level}
              </Badge>
              <span className="font-mono text-sm font-semibold text-accent-gold">{a.ts_code}</span>
              <span className="text-sm text-text-secondary">{a.alert_type}</span>
              <span className="text-xs text-text-muted flex-1 truncate">{a.message}</span>
              <IconArrowRight size={15} className="shrink-0 text-text-muted opacity-0 -translate-x-1 transition-all group-hover:opacity-100 group-hover:translate-x-0 group-hover:text-accent-gold" />
            </div>
          ))}
        </div>
      )}

      {scanResult && scanResult.alerts.length === 0 && (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border/50 py-12 text-text-muted animate-fade-up animate-fade-up-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-full bg-accent-green/10 ring-1 ring-inset ring-accent-green/25 text-accent-green mb-3">
            <IconZap size={18} />
          </div>
          <div className="text-sm font-medium text-text-secondary">暂无信号 — 自选股平稳运行中</div>
          <div className="text-xs mt-1 text-text-muted/80">有新信号时会在此处出现</div>
        </div>
      )}

      {!scanResult && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 animate-fade-up animate-fade-up-3">
          {quickActions.map((item) => (
            <button
              key={item.path}
              onClick={() => navigate(item.path)}
              className="card-shine group relative overflow-hidden rounded-xl border border-border/50 bg-bg-card/60 p-5 text-left transition-all duration-300 hover:border-accent-gold/30 hover:bg-bg-hover/30 hover:-translate-y-0.5 hover:shadow-xl hover:shadow-black/30"
            >
              <div className="flex items-start justify-between">
                <div className={`flex h-10 w-10 items-center justify-center rounded-lg ring-1 ring-inset ${actionTone[item.tone]}`}>
                  {item.icon}
                </div>
                <IconArrowRight size={16} className="text-text-muted opacity-0 -translate-x-1 transition-all duration-300 group-hover:opacity-100 group-hover:translate-x-0 group-hover:text-accent-gold" />
              </div>
              <div className="mt-3.5 text-sm font-semibold text-text-primary">{item.title}</div>
              <div className="mt-1 text-xs text-text-muted">{item.desc}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
