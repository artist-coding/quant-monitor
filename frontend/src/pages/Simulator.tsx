import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { runSimulation } from '../api/simulator';
import type { SimulationRequest, SimulationResponse } from '../api/types';
import Card from '../components/ui/Card';
import Button from '../components/ui/Button';
import PageHeader from '../components/ui/PageHeader';
import StatCard from '../components/ui/StatCard';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import SimulatorEquityCurveChart from '../components/charts/SimulatorEquityCurveChart';
import { formatNumber, formatPct } from '../lib/formatters';

const DAY_OPTIONS = [120, 250, 365, 500, 730];

function parseTsCodes(input: string): string[] {
  if (!input.trim()) return [];
  return input
    .split(/[,，\s]+/)
    .map((c) => c.trim())
    .filter(Boolean)
    .map((c) => {
      if (/^\d{6}$/.test(c)) {
        return c.startsWith('6') ? `${c}.SH` : `${c}.SZ`;
      }
      return c.toUpperCase();
    });
}

export default function Simulator() {
  const [tsCodesInput, setTsCodesInput] = useState('');
  const [days, setDays] = useState(250);
  const [capital, setCapital] = useState(1_000_000);
  const [strategyMode, setStrategyMode] = useState<'simple' | 'resonance'>('simple');
  const [atrSizing, setAtrSizing] = useState(false);
  const [result, setResult] = useState<SimulationResponse | null>(null);

  const mutation = useMutation({
    mutationFn: () => {
      const params: SimulationRequest = {
        ts_codes: parseTsCodes(tsCodesInput),
        days,
        capital,
        max_positions: 5,
        risk_per_trade: 0.02,
        min_score: 60,
        min_signals: 2,
        atr_sizing: atrSizing,
        max_position_pct: 0.15,
        benchmark: '000300.SH',
        cost_model: 'realistic',
        slippage: 'dynamic',
        no_st: false,
        strategy_mode: strategyMode,
        strategy_lookback: 5,
        min_resonance_score: 50,
        walk_forward: false,
        wf_train_days: 120,
        wf_test_days: 60,
        wf_objective: 'calmar',
      };
      return runSimulation(params);
    },
    onSuccess: (data) => setResult(data),
  });

  const handleRun = () => {
    mutation.mutate();
  };

  const metrics = result?.metrics;
  const summary = result?.summary as Record<string, number> | undefined;

  const summaryItems = result
    ? [
        {
          label: '年化收益',
          value: formatPct(((metrics?.annualized_return ?? 0) as number) * 100),
          tone: 'red' as const,
          cls: (metrics?.annualized_return ?? 0) >= 0 ? 'text-up' : 'text-down',
        },
        { label: '夏普比率', value: formatNumber(metrics?.sharpe_ratio ?? 0), tone: 'blue' as const, cls: undefined },
        { label: 'Calmar', value: formatNumber(metrics?.calmar_ratio ?? 0), tone: 'purple' as const, cls: undefined },
        {
          label: '最大回撤',
          value: formatPct(-Math.abs((metrics?.max_drawdown ?? 0) as number) * 100),
          tone: 'green' as const,
          cls: 'text-down',
        },
        { label: '胜率', value: formatPct(((metrics?.win_rate ?? 0) as number) * 100), tone: 'gold' as const, cls: undefined },
        { label: '总交易数', value: String(summary?.total_trades ?? 0), tone: 'cyan' as const, cls: undefined },
      ]
    : [];

  return (
    <div className="space-y-5 animate-fade-up">
      <PageHeader title="端到端模拟" description="组合级策略仿真，含基准对比与回撤分析" />

      {/* Config */}
      <Card title="模拟配置">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <div className="lg:col-span-2">
            <label className="block text-xs font-medium text-text-muted mb-1.5">股票代码（逗号/空格分隔，留空=全市场）</label>
            <input
              type="text"
              value={tsCodesInput}
              onChange={(e) => setTsCodesInput(e.target.value)}
              placeholder="如 000001.SZ, 600487.SH"
              className="input-dark w-full font-mono"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-text-muted mb-1.5">回测天数</label>
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              className="input-dark w-full"
            >
              {DAY_OPTIONS.map((n) => (
                <option key={n} value={n}>{n} 天</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-text-muted mb-1.5">初始资金</label>
            <input
              type="number"
              value={capital}
              onChange={(e) => setCapital(Number(e.target.value))}
              min={10000}
              step={100000}
              className="input-dark w-full font-mono"
            />
          </div>
        </div>

        <div className="mt-5 flex flex-wrap items-center gap-5 border-t border-border/40 pt-4">
          <div className="flex items-center gap-3">
            <span className="text-xs text-text-muted">策略模式</span>
            <div className="flex rounded-lg bg-bg-primary/70 border border-border/60 p-0.5">
              {(
                [
                  { key: 'simple', label: '简单' },
                  { key: 'resonance', label: '战法共振' },
                ] as const
              ).map((m) => (
                <button
                  key={m.key}
                  type="button"
                  onClick={() => setStrategyMode(m.key)}
                  className={`rounded-md px-3.5 py-1.5 text-xs font-medium transition-all ${
                    strategyMode === m.key
                      ? 'bg-accent-gold/15 text-accent-gold shadow-[inset_0_0_0_1px_rgba(245,185,66,0.35)]'
                      : 'text-text-secondary hover:text-text-primary'
                  }`}
                >
                  {m.label}
                </button>
              ))}
            </div>
          </div>

          <label className="flex cursor-pointer items-center gap-2">
            <input
              type="checkbox"
              checked={atrSizing}
              onChange={(e) => setAtrSizing(e.target.checked)}
              className="h-4 w-4 accent-accent-gold"
            />
            <span className="text-xs text-text-secondary">ATR 动态仓位</span>
          </label>

          <div className="flex-1" />

          <Button size="lg" onClick={handleRun} disabled={mutation.isPending}>
            {mutation.isPending ? '模拟中...' : '▶ 开始模拟'}
          </Button>
        </div>
      </Card>

      {/* Loading */}
      {mutation.isPending && (
        <div className="flex items-center justify-center py-16">
          <LoadingSpinner size="lg" />
        </div>
      )}

      {/* Results */}
      {result && !mutation.isPending && (
        <>
          {/* Summary Cards */}
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4">
            {summaryItems.map((item) => (
              <StatCard key={item.label} label={item.label} value={item.value} tone={item.tone} valueClassName={item.cls} />
            ))}
          </div>

          {/* Equity Curve */}
          <Card title="资金曲线 / 回撤">
            <SimulatorEquityCurveChart
              equityCurve={result.equity_curve}
              benchmarkCurve={result.benchmark_curve}
              height={360}
            />
          </Card>

          {/* Trade Table */}
          <Card title={`交易明细 (${result.trades.length} 笔)`}>
            <div className="overflow-x-auto max-h-96 overflow-y-auto -mx-5 -mb-5">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-bg-elevated z-10">
                  <tr className="border-b border-border/60">
                    <th className="text-left px-5 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">日期</th>
                    <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">代码</th>
                    <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">操作</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">价格</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">股数</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">盈亏</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">手续费</th>
                    <th className="text-left px-5 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">原因</th>
                  </tr>
                </thead>
                <tbody>
                  {result.trades.map((t, i) => (
                    <tr key={i} className="border-b border-border/30 last:border-0 transition-colors hover:bg-bg-hover/30">
                      <td className="px-5 py-2.5 text-text-secondary font-mono">{t.date}</td>
                      <td className="px-3 py-2.5 font-mono text-accent-gold">{t.ts_code}</td>
                      <td
                        className={`px-3 py-2.5 font-bold ${t.action === 'BUY'
                          ? 'text-up'
                          : t.action === 'SELL' || t.action === 'PARTIAL_SELL'
                            ? 'text-down'
                            : 'text-text-secondary'
                          }`}
                      >
                        {t.action}
                      </td>
                      <td className="px-3 py-2.5 text-right font-mono text-text-primary tabular-nums">{formatNumber(t.price, 3)}</td>
                      <td className="px-3 py-2.5 text-right font-mono text-text-secondary tabular-nums">{t.shares}</td>
                      <td
                        className={`px-3 py-2.5 text-right font-mono font-bold tabular-nums ${(t.pnl ?? 0) >= 0 ? 'text-up' : 'text-down'}`}
                      >
                        {t.pnl != null ? formatPct(t.pnl_pct ?? 0) : '--'}
                      </td>
                      <td className="px-3 py-2.5 text-right font-mono text-text-secondary tabular-nums">{formatNumber(t.fee)}</td>
                      <td className="px-5 py-2.5 text-text-muted">{t.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
