import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { runScreen } from '../api/screen';
import Card from '../components/ui/Card';
import Button from '../components/ui/Button';
import PageHeader from '../components/ui/PageHeader';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import ApiErrorState from '../components/ui/ApiErrorState';
import { IconTarget, IconArrowRight } from '../components/ui/icons';
import { STRATEGIES } from '../lib/constants';
import { formatNumber } from '../lib/formatters';

export default function Screener() {
  const navigate = useNavigate();
  const [selected, setSelected] = useState('B1');
  const [limit, setLimit] = useState(20);
  const [ran, setRan] = useState(false);

  const { data: result, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['screen', selected, limit],
    queryFn: () => runScreen(selected, limit),
    enabled: false,
  });

  const handleRun = async () => {
    setRan(true);
    await refetch();
  };

  return (
    <div className="space-y-5 animate-fade-up">
      <PageHeader
        title="选股筛选"
        description="选择战法扫描全市场，命中即入候选池"
      />

      {/* Strategy Selector */}
      <Card>
        <div className="flex flex-wrap gap-2 mb-5">
          {STRATEGIES.map((s) => (
            <button
              key={s.alias}
              onClick={() => setSelected(s.alias)}
              className={`rounded-full px-3.5 py-1.5 text-[13px] font-medium transition-all duration-200 ${
                selected === s.alias
                  ? 'bg-accent-gold/15 text-accent-gold ring-1 ring-inset ring-accent-gold/50 shadow-[0_0_14px_-4px_rgba(245,185,66,0.4)]'
                  : 'bg-bg-hover/40 text-text-secondary ring-1 ring-inset ring-border/60 hover:text-text-primary hover:ring-border'
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <span className="text-xs text-text-muted">数量</span>
            <select
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
              className="input-dark"
            >
              {[10, 20, 50, 100].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </div>
          <Button onClick={handleRun} disabled={isLoading}>
            {isLoading ? '筛选中...' : '开始筛选'}
          </Button>
        </div>
      </Card>

      {/* Results */}
      {isLoading && (
        <div className="flex items-center justify-center py-16">
          <LoadingSpinner size="lg" />
        </div>
      )}

      {ran && isError && !isLoading && (
        <ApiErrorState
          message={(error as Error)?.message || '筛选失败'}
          onRetry={() => refetch()}
        />
      )}

      {/* 初始引导:用户还没跑过筛选时显示 */}
      {!ran && !result && (
        <Card>
          <div className="flex flex-col items-center text-center py-10">
            <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-accent-gold/10 ring-1 ring-inset ring-accent-gold/25 text-accent-gold mb-4">
              <IconTarget size={22} />
            </div>
            <div className="text-sm font-semibold text-text-primary mb-1.5">选择战法开始筛选</div>
            <div className="text-xs text-text-muted max-w-md leading-relaxed">在上方选择战法(如 B1 / B2 / 长安战法 等),设定数量后点击"开始筛选",系统会扫描全市场命中该战法的个股。</div>
          </div>
        </Card>
      )}

      {ran && result && !isLoading && (
        <Card title={`筛选结果 — ${result.strategy} (${result.count} 只)`}>
          {result.stocks.length === 0 ? (
            <div className="text-center py-10 text-sm text-text-muted">无符合条件的股票</div>
          ) : (
            <div className="overflow-x-auto -mx-5 -mb-5">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border/60 bg-bg-secondary/40">
                    <th className="text-left px-5 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">代码</th>
                    <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">名称</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">总分</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">B1</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">趋势</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">量价</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">风险</th>
                    <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">评级</th>
                    <th className="text-right px-5 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {result.stocks.map((s) => (
                    <tr key={s.ts_code} className="border-b border-border/30 last:border-0 transition-colors hover:bg-bg-hover/30">
                      <td className="px-5 py-2.5 font-mono font-medium text-accent-gold">{s.ts_code}</td>
                      <td className="px-3 py-2.5 text-text-primary">{s.name}</td>
                      <td className="px-3 py-2.5 text-right font-mono font-bold text-text-primary tabular-nums">{formatNumber(s.score, 1)}</td>
                      <td className="px-3 py-2.5 text-right font-mono text-text-secondary tabular-nums">{formatNumber(s.b1_score, 1)}</td>
                      <td className="px-3 py-2.5 text-right font-mono text-text-secondary tabular-nums">{formatNumber(s.trend_score, 1)}</td>
                      <td className="px-3 py-2.5 text-right font-mono text-text-secondary tabular-nums">{formatNumber(s.volume_score, 1)}</td>
                      <td className="px-3 py-2.5 text-right font-mono text-text-secondary tabular-nums">{formatNumber(s.risk_score, 1)}</td>
                      <td className="px-3 py-2.5 text-text-secondary">{s.rating}</td>
                      <td className="px-5 py-2.5 text-right">
                        <button
                          onClick={() => navigate(`/stock/${s.ts_code}`)}
                          className="inline-flex items-center gap-1 text-xs font-medium text-accent-blue hover:text-accent-gold transition-colors"
                        >
                          分析
                          <IconArrowRight size={12} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
