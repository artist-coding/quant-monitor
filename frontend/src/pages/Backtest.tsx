import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { runShaofu } from '../api/backtest';
import type { BacktestResult } from '../api/types';
import Card from '../components/ui/Card';
import Button from '../components/ui/Button';
import PageHeader from '../components/ui/PageHeader';
import StatCard from '../components/ui/StatCard';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import EquityCurveChart from '../components/charts/EquityCurveChart';
import { formatNumber, formatPct } from '../lib/formatters';

export default function Backtest() {
  const [tsCode, setTsCode] = useState('');
  const [days, setDays] = useState(250);
  const [result, setResult] = useState<BacktestResult | null>(null);

  const mutation = useMutation({
    mutationFn: () => runShaofu({ ts_code: tsCode, days }),
    onSuccess: (data) => setResult(data),
  });

  const handleRun = () => {
    if (!tsCode.trim()) return;
    let code = tsCode.trim().toUpperCase();
    if (/^\d{6}$/.test(code)) {
      code = code.startsWith('6') ? `${code}.SH` : `${code}.SZ`;
    }
    setTsCode(code);
    mutation.mutate();
  };

  const summaryItems = result
    ? [
        { label: '总收益', value: formatPct(result.summary.total_return), tone: 'red' as const, cls: result.summary.total_return >= 0 ? 'text-up' : 'text-down' },
        { label: '胜率', value: `${(result.summary.win_rate * 100).toFixed(1)}%`, tone: 'gold' as const, cls: undefined },
        { label: '盈亏比', value: formatNumber(result.summary.profit_factor), tone: 'blue' as const, cls: undefined },
        { label: '最大回撤', value: formatPct(-Math.abs(result.summary.max_drawdown)), tone: 'green' as const, cls: 'text-down' },
        { label: '夏普比率', value: formatNumber(result.summary.sharpe_ratio), tone: 'purple' as const, cls: undefined },
        { label: '交易次数', value: String(result.summary.total_trades), tone: 'cyan' as const, cls: undefined },
      ]
    : [];

  return (
    <div className="space-y-5 animate-fade-up">
      <PageHeader title="策略回测" description="少妇战法历史收益验证" />

      {/* Config */}
      <Card title="回测配置">
        <div className="flex flex-wrap items-end gap-4">
          <div className="flex-1 min-w-52">
            <label className="block text-xs font-medium text-text-muted mb-1.5">股票代码</label>
            <input
              type="text"
              value={tsCode}
              onChange={(e) => setTsCode(e.target.value)}
              placeholder="如 600487.SH"
              className="input-dark w-full font-mono"
              onKeyDown={(e) => { if (e.key === 'Enter') handleRun(); }}
            />
          </div>
          <div className="w-36">
            <label className="block text-xs font-medium text-text-muted mb-1.5">回测天数</label>
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              className="input-dark w-full"
            >
              {[120, 250, 365, 500, 730].map((n) => (
                <option key={n} value={n}>{n} 天</option>
              ))}
            </select>
          </div>
          <Button size="lg" onClick={handleRun} disabled={mutation.isPending || !tsCode.trim()}>
            {mutation.isPending ? '回测中...' : '▶ 开始回测'}
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
          <Card title="资金曲线">
            <EquityCurveChart equityCurve={result.equity_curve} height={300} />
          </Card>

          {/* Trade Table */}
          <Card title={`交易明细 (${result.trades.length} 笔)`}>
            <div className="overflow-x-auto max-h-96 overflow-y-auto -mx-5 -mb-5">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-bg-elevated z-10">
                  <tr className="border-b border-border/60">
                    <th className="text-left px-5 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">买入日期</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">买入价</th>
                    <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">卖出日期</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">卖出价</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">盈亏</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">持仓天数</th>
                    <th className="text-left px-5 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">退出原因</th>
                  </tr>
                </thead>
                <tbody>
                  {result.trades.map((t, i) => (
                    <tr key={i} className="border-b border-border/30 last:border-0 transition-colors hover:bg-bg-hover/30">
                      <td className="px-5 py-2.5 text-text-secondary font-mono">{t.entry_date}</td>
                      <td className="px-3 py-2.5 text-right font-mono text-text-primary tabular-nums">{formatNumber(t.entry_price)}</td>
                      <td className="px-3 py-2.5 text-text-secondary font-mono">{t.exit_date || '--'}</td>
                      <td className="px-3 py-2.5 text-right font-mono text-text-primary tabular-nums">{t.exit_price ? formatNumber(t.exit_price) : '--'}</td>
                      <td className={`px-3 py-2.5 text-right font-mono font-bold tabular-nums ${t.pnl_pct >= 0 ? 'text-up' : 'text-down'}`}>
                        {formatPct(t.pnl_pct)}
                      </td>
                      <td className="px-3 py-2.5 text-right text-text-secondary tabular-nums">{t.holding_days}</td>
                      <td className="px-5 py-2.5 text-text-muted">{t.exit_reason}</td>
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
