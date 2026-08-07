import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchTrades, addTrade, deleteTrade, fetchTradeStats } from '../api/trade';
import Card from '../components/ui/Card';
import Button from '../components/ui/Button';
import Badge from '../components/ui/Badge';
import PageHeader from '../components/ui/PageHeader';
import StatCard from '../components/ui/StatCard';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import { IconExchange, IconChart, IconStar, IconZap } from '../components/ui/icons';
import { formatNumber } from '../lib/formatters';

export default function Trades() {
  const queryClient = useQueryClient();
  const [inputText, setInputText] = useState('');
  const [page, setPage] = useState(1);

  const { data: tradeList, isLoading } = useQuery({
    queryKey: ['trades', page],
    queryFn: () => fetchTrades(page),
  });

  const { data: stats } = useQuery({
    queryKey: ['trade-stats'],
    queryFn: fetchTradeStats,
  });

  const addMutation = useMutation({
    mutationFn: (text: string) => addTrade(text),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['trades'] });
      queryClient.invalidateQueries({ queryKey: ['trade-stats'] });
      setInputText('');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteTrade,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['trades'] }),
  });

  if (isLoading) {
    return <div className="flex items-center justify-center h-96"><LoadingSpinner size="lg" /></div>;
  }

  const pnlMap = (stats?.pnl as Record<string, number>) || {};
  const pnl = pnlMap.realized_pnl || 0;
  const hasTrades = (pnlMap.buy_total || 0) > 0 || (pnlMap.sell_total || 0) > 0;
  const pnlTone = !hasTrades ? 'text-text-muted' : pnl > 0 ? 'text-up' : pnl < 0 ? 'text-down' : 'text-text-primary';

  return (
    <div className="space-y-5 animate-fade-up">
      <PageHeader title="交易记录" description="口语化记账，自动汇总盈亏" />

      {/* Input */}
      <Card title="记录交易">
        <div className="flex gap-3">
          <input
            type="text"
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            placeholder="口语化输入，如：4月25号买了100股茅台，1800块"
            className="input-dark flex-1"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && inputText.trim()) {
                addMutation.mutate(inputText);
              }
            }}
          />
          <Button
            onClick={() => addMutation.mutate(inputText)}
            disabled={!inputText.trim() || addMutation.isPending}
          >
            保存
          </Button>
        </div>
        {addMutation.isSuccess && (
          <div className="mt-2.5 flex items-center gap-1.5 text-xs text-accent-green">
            <span className="h-1 w-1 rounded-full bg-accent-green" />
            保存成功
          </div>
        )}
        {addMutation.isError && (
          <div className="mt-2.5 flex items-center gap-1.5 text-xs text-accent-red">
            <span className="h-1 w-1 rounded-full bg-accent-red" />
            保存失败
          </div>
        )}
      </Card>

      {/* Stats */}
      {stats && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard label="买入总额" value={formatNumber(pnlMap.buy_total || 0)} icon={<IconZap size={18} />} tone="red" valueClassName="text-text-primary" />
          <StatCard label="卖出总额" value={formatNumber(pnlMap.sell_total || 0)} icon={<IconChart size={18} />} tone="green" valueClassName="text-text-primary" />
          <StatCard label="当前持仓" value={pnlMap.current_qty || 0} suffix="股" icon={<IconStar size={18} />} tone="gold" />
          <StatCard label="已实现盈亏" value={formatNumber(pnl)} icon={<IconExchange size={18} />} tone="blue" valueClassName={pnlTone} />
        </div>
      )}

      {/* Trade List */}
      <Card title={`交易记录 (${tradeList?.total || 0})`}>
        {!tradeList || tradeList.records.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-text-muted">
            <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-accent-blue/10 ring-1 ring-inset ring-accent-blue/25 text-accent-blue mb-4">
              <IconExchange size={22} />
            </div>
            <div className="text-sm font-medium text-text-secondary">暂无交易记录</div>
            <div className="text-xs mt-1.5 text-text-muted/80">在上方用一句话记下你的第一笔交易</div>
          </div>
        ) : (
          <>
            <div className="overflow-x-auto -mx-5">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border/60 bg-bg-secondary/40">
                    <th className="text-left px-5 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">日期</th>
                    <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">代码</th>
                    <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">方向</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">价格</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">数量</th>
                    <th className="text-right px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">金额</th>
                    <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">原因</th>
                    <th className="text-right px-5 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {tradeList.records.map((r) => (
                    <tr key={r.id} className="border-b border-border/30 last:border-0 transition-colors hover:bg-bg-hover/30">
                      <td className="px-5 py-2.5 text-text-secondary font-mono text-xs">{r.trade_date}</td>
                      <td className="px-3 py-2.5 font-mono font-medium text-accent-gold">{r.ts_code}</td>
                      <td className="px-3 py-2.5">
                        <Badge variant={r.action === 'BUY' ? 'danger' : 'success'} dot>
                          {r.action === 'BUY' ? '买入' : '卖出'}
                        </Badge>
                      </td>
                      <td className="px-3 py-2.5 text-right font-mono text-text-primary tabular-nums">{formatNumber(r.price)}</td>
                      <td className="px-3 py-2.5 text-right font-mono text-text-secondary tabular-nums">{r.quantity}</td>
                      <td className="px-3 py-2.5 text-right font-mono text-text-primary tabular-nums">{formatNumber(r.amount)}</td>
                      <td className="px-3 py-2.5 text-text-muted text-xs max-w-48 truncate">{r.reason}</td>
                      <td className="px-5 py-2.5 text-right">
                        <button
                          onClick={() => deleteMutation.mutate(r.id)}
                          className="text-xs font-medium text-text-muted hover:text-accent-red transition-colors"
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {/* Pagination */}
            <div className="flex items-center justify-between mt-4 pt-4 border-t border-border/40">
              <span className="text-xs text-text-muted font-mono">
                第 {tradeList.page} 页 / 共 {tradeList.total} 条
              </span>
              <div className="flex gap-2">
                <Button size="sm" variant="secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                  上一页
                </Button>
                <Button size="sm" variant="secondary" disabled={page * tradeList.page_size >= tradeList.total} onClick={() => setPage(page + 1)}>
                  下一页
                </Button>
              </div>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
