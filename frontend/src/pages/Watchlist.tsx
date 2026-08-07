import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { fetchWatchlist, addToWatchlist, refreshWatchlist, removeFromWatchlist, scanWatchlist } from '../api/watchlist';
import type { WatchlistList } from '../api/types';
import Card from '../components/ui/Card';
import Button from '../components/ui/Button';
import Badge from '../components/ui/Badge';
import PageHeader from '../components/ui/PageHeader';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import ApiErrorState from '../components/ui/ApiErrorState';
import { IconStar, IconArrowRight, IconRefresh } from '../components/ui/icons';
import { formatPct, formatPrice, pctColor, ratingStars } from '../lib/formatters';

function scoreVariant(score: number): 'success' | 'info' | 'warning' | 'danger' {
  if (score >= 65) return 'success';
  if (score >= 50) return 'info';
  if (score >= 35) return 'warning';
  return 'danger';
}

export default function Watchlist() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [newCode, setNewCode] = useState('');

  const { data: watchlist, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['watchlist'],
    queryFn: fetchWatchlist,
  });

  const { data: scanResult, isFetching: scanning, refetch: runScan } = useQuery({
    queryKey: ['watchlist-scan'],
    queryFn: scanWatchlist,
    enabled: false,
  });

  const addMutation = useMutation({
    mutationFn: (code: string) => addToWatchlist(code),
    onSuccess: async (result) => {
      if (result.item) {
        queryClient.setQueryData<WatchlistList>(['watchlist'], (current) => {
          const existing = current?.items.filter((item) => item.ts_code !== result.item?.ts_code) ?? [];
          return { count: existing.length + 1, items: [result.item!, ...existing] };
        });
      }
      setNewCode('');
      await queryClient.invalidateQueries({ queryKey: ['watchlist'] });
    },
  });

  const removeMutation = useMutation({
    mutationFn: removeFromWatchlist,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['watchlist'] }),
  });

  const refreshMutation = useMutation({
    mutationFn: () => refreshWatchlist(250),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['watchlist'] });
      await queryClient.invalidateQueries({ queryKey: ['watchlist-scan'] });
    },
  });

  const handleScan = () => {
    runScan();
  };

  if (isLoading) {
    return <div className="flex items-center justify-center h-96"><LoadingSpinner size="lg" /></div>;
  }

  if (isError) {
    return (
      <ApiErrorState
        message={(error as Error)?.message || '加载自选股失败'}
        onRetry={() => refetch()}
      />
    );
  }

  return (
    <div className="space-y-5 animate-fade-up">
      <PageHeader
        title="自选股管理"
        description="看行情、系统评分与交易信号"
        actions={
          <div className="flex items-center gap-2">
            <Button variant="secondary" onClick={() => refreshMutation.mutate()} disabled={refreshMutation.isPending}>
              <IconRefresh size={14} />
              {refreshMutation.isPending ? '更新中...' : '刷新行情与指标'}
            </Button>
            <Button variant="secondary" onClick={handleScan} disabled={scanning}>
              {scanning ? '扫描中...' : '信号扫描'}
            </Button>
          </div>
        }
      />

      {refreshMutation.isSuccess && (
        <div className="rounded-lg border border-accent-green/25 bg-accent-green/8 px-4 py-3 text-xs text-accent-green">
          已刷新 {refreshMutation.data.stocks} 只股票，写入 {refreshMutation.data.kline_rows} 根日线，更新 {refreshMutation.data.indicator_rows} 条指标。
        </div>
      )}
      {refreshMutation.isError && (
        <div className="rounded-lg border border-accent-red/25 bg-accent-red/8 px-4 py-3 text-xs text-accent-red">
          刷新失败：{refreshMutation.error.message}
        </div>
      )}

      {/* Add */}
      <Card title="添加自选股">
        <div className="flex flex-wrap items-center gap-3">
          <input
            type="text"
            value={newCode}
            onChange={(e) => setNewCode(e.target.value)}
            placeholder="股票代码，如 600487 或 600487.SH"
            className="input-dark min-w-0 flex-1 basis-48 font-mono"
          />
          <Button onClick={() => addMutation.mutate(newCode)} disabled={!newCode.trim() || addMutation.isPending} className="shrink-0">
            {addMutation.isPending ? '召回中...' : '添加'}
          </Button>
        </div>
        {addMutation.isSuccess && (
          <div className="mt-3 text-xs text-accent-green">{addMutation.data.message || '添加成功，已重新读取本地数据库'}</div>
        )}
        {addMutation.isError && (
          <div className="mt-3 text-xs text-accent-red">添加失败：{addMutation.error.message}</div>
        )}
      </Card>

      {/* Scan Results */}
      {scanResult && scanResult.alerts.length > 0 && (
        <Card title={`扫描结果 — ${scanResult.total} 只，${scanResult.alerts.length} 个信号`}>
          <div className="space-y-2">
            {scanResult.alerts.map((a, i) => (
              <div key={i} className="flex items-center gap-3 rounded-lg bg-bg-hover/30 px-3.5 py-2.5 ring-1 ring-inset ring-border/30">
                <Badge variant={a.level === 'CRITICAL' ? 'danger' : a.level === 'WARNING' ? 'warning' : 'info'} dot>
                  {a.level}
                </Badge>
                <span className="text-xs font-mono font-semibold text-accent-gold">{a.ts_code}</span>
                <span className="text-xs text-text-secondary">{a.alert_type}</span>
                <span className="text-xs text-text-muted flex-1 truncate">{a.message}</span>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* List */}
      <Card title={`自选股列表 (${watchlist?.count || 0})`}>
        {!watchlist || watchlist.items.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-text-muted">
            <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-accent-gold/10 ring-1 ring-inset ring-accent-gold/25 text-accent-gold mb-4">
              <IconStar size={22} />
            </div>
            <div className="text-sm font-medium text-text-secondary">暂无自选股</div>
            <div className="text-xs mt-1.5 text-text-muted/80">在上方输入股票代码即可加入观察池</div>
          </div>
        ) : (
          <div className="overflow-x-auto -mx-5 -mb-5">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border/60 bg-bg-secondary/40">
                  <th className="text-left px-5 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">股票</th>
                  <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">最新行情</th>
                  <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">系统评分</th>
                  <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">四维评分</th>
                  <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">核心指标</th>
                  <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">趋势 / 信号</th>
                  <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">数据日期</th>
                  <th className="text-right px-5 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">操作</th>
                </tr>
              </thead>
              <tbody>
                {watchlist.items.map((item) => (
                  <tr key={item.ts_code} className="border-b border-border/30 last:border-0 transition-colors hover:bg-bg-hover/30">
                    <td className="px-5 py-3">
                      <div className="font-medium text-text-primary">{item.name || '--'}</div>
                      <div className="mt-0.5 font-mono text-xs text-accent-gold">{item.ts_code}</div>
                    </td>
                    <td className="px-3 py-3 text-right">
                      <div className="font-mono font-semibold text-text-primary">{formatPrice(item.price)}</div>
                      <div className={`mt-0.5 font-mono text-xs font-medium ${pctColor(item.pct_chg ?? 0)}`}>
                        {item.pct_chg === null ? '--' : formatPct(item.pct_chg)}
                      </div>
                    </td>
                    <td className="px-3 py-3">
                      {item.data_ready ? (
                        <div>
                          <Badge variant={scoreVariant(item.score)}>{item.score.toFixed(1)} 分</Badge>
                          <div className="mt-1 text-[11px] tracking-wide text-accent-gold">{ratingStars(item.rating)}</div>
                        </div>
                      ) : <Badge variant="warning">待补历史</Badge>}
                    </td>
                    <td className="px-3 py-3">
                      {item.data_ready ? (
                        <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono text-[11px] text-text-secondary">
                          <span>B1 {item.b1_score.toFixed(0)}</span>
                          <span>趋势 {item.trend_score.toFixed(0)}</span>
                          <span>量价 {item.volume_score.toFixed(0)}</span>
                          <span>风险 {item.risk_score.toFixed(0)}</span>
                        </div>
                      ) : <span className="text-xs text-text-muted">--</span>}
                    </td>
                    <td className="px-3 py-3">
                      {item.data_ready ? (
                        <div className="space-y-0.5 font-mono text-[11px] text-text-secondary">
                          <div>J值 <span className="text-text-primary">{item.j?.toFixed(1) ?? '--'}</span></div>
                          <div>量比 <span className="text-text-primary">{item.vol_ratio?.toFixed(2) ?? '--'}x</span></div>
                        </div>
                      ) : <span className="text-xs text-text-muted">--</span>}
                    </td>
                    <td className="px-3 py-3">
                      <div className="flex flex-wrap gap-1.5">
                        <Badge variant={item.trend_status === '强势' ? 'success' : item.trend_status === '弱势' ? 'danger' : 'default'}>
                          {item.trend_status}
                        </Badge>
                        {item.data_ready && <Badge variant={item.macd_status === '金叉' || item.macd_status === '偏多' ? 'success' : 'warning'}>{item.macd_status}</Badge>}
                        {item.data_ready && <Badge variant={item.signal === 'BUY' ? 'success' : item.signal === 'SELL' ? 'danger' : 'info'}>{item.signal}</Badge>}
                      </div>
                    </td>
                    <td className="px-3 py-3 text-text-muted text-xs font-mono">
                      <div>{item.trade_date || '--'}</div>
                      <div className="mt-0.5 text-[10px]">{item.kline_count} 根K线</div>
                    </td>
                    <td className="px-5 py-3 text-right">
                      <div className="inline-flex items-center gap-3">
                        <button
                          onClick={() => navigate(`/stock/${item.ts_code}`)}
                          className="inline-flex items-center gap-1 text-xs font-medium text-accent-blue hover:text-accent-gold transition-colors"
                        >
                          分析
                          <IconArrowRight size={12} />
                        </button>
                        <button
                          onClick={() => removeMutation.mutate(item.ts_code)}
                          className="text-xs font-medium text-text-muted hover:text-accent-red transition-colors"
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
