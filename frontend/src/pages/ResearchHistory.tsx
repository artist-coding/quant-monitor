import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { fetchResearchHistory, type ResearchStatus, type ResearchTaskSummary } from '../api/research';
import ApiErrorState from '../components/ui/ApiErrorState';
import Badge from '../components/ui/Badge';
import Button from '../components/ui/Button';
import Card from '../components/ui/Card';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import PageHeader from '../components/ui/PageHeader';
import {
  IconAlert,
  IconArrowRight,
  IconCheck,
  IconClock,
  IconHistory,
  IconRadar,
  IconRefresh,
  IconSearch,
} from '../components/ui/icons';

type StatusFilter = 'all' | ResearchStatus;

const STATUS_OPTIONS: Array<{ value: StatusFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'completed', label: '已完成' },
  { value: 'running', label: '进行中' },
  { value: 'queued', label: '排队中' },
  { value: 'failed', label: '未完成' },
];

const STATUS_LABELS: Record<ResearchStatus, string> = {
  queued: '排队中',
  running: '进行中',
  completed: '已完成',
  failed: '未完成',
};

function formatDate(value: string): string {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function statusVariant(status: ResearchStatus): 'success' | 'warning' | 'danger' {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'danger';
  return 'warning';
}

function StatusIcon({ task }: { task: ResearchTaskSummary }) {
  const classes = task.status === 'completed'
    ? 'bg-accent-green/10 text-accent-green ring-accent-green/25'
    : task.status === 'failed'
      ? 'bg-accent-red/10 text-accent-red ring-accent-red/25'
      : 'bg-accent-gold/10 text-accent-gold ring-accent-gold/25';
  return (
    <span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ring-1 ring-inset ${classes}`}>
      {task.status === 'completed' ? <IconCheck size={19} /> : task.status === 'failed' ? <IconAlert size={19} /> : <IconRadar size={19} />}
    </span>
  );
}

export default function ResearchHistory() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<StatusFilter>('all');
  const [draftKeyword, setDraftKeyword] = useState('');
  const [keyword, setKeyword] = useState('');
  const pageSize = 15;
  const query = useQuery({
    queryKey: ['research-history', page, status, keyword],
    queryFn: () => fetchResearchHistory({
      page,
      pageSize,
      status: status === 'all' ? undefined : status,
      keyword,
    }),
    refetchInterval: (result) => result.state.data?.tasks.some((task) => task.status === 'running' || task.status === 'queued') ? 3000 : false,
  });

  const data = query.data;
  const totalPages = Math.max(1, Math.ceil((data?.total || 0) / pageSize));

  if (query.isError) {
    return <ApiErrorState message={(query.error as Error)?.message || '读取分析记录失败'} onRetry={() => query.refetch()} />;
  }

  const submitSearch = (event: React.FormEvent) => {
    event.preventDefault();
    setPage(1);
    setKeyword(draftKeyword.trim());
  };

  return (
    <div className="space-y-5 animate-fade-up">
      <PageHeader
        title="分析记录"
        description="服务器永久保存的股票与行业调研，可随时返回查看"
        actions={<Button onClick={() => navigate('/')}>开始新分析</Button>}
      />

      <Card>
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex flex-wrap gap-2">
            {STATUS_OPTIONS.map((option) => {
              const active = status === option.value;
              const count = data?.status_counts?.[option.value];
              return (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => {
                    setStatus(option.value);
                    setPage(1);
                  }}
                  className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${active ? 'border-accent-gold/40 bg-accent-gold/10 text-accent-gold' : 'border-border/60 bg-bg-primary/30 text-text-muted hover:border-border hover:text-text-primary'}`}
                >
                  {option.label}{typeof count === 'number' ? ` ${count}` : ''}
                </button>
              );
            })}
          </div>
          <form onSubmit={submitSearch} className="flex w-full gap-2 lg:w-auto">
            <div className="relative min-w-0 flex-1 lg:w-72">
              <IconSearch size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
              <input
                value={draftKeyword}
                onChange={(event) => setDraftKeyword(event.target.value)}
                placeholder="搜索股票、行业或主题"
                className="input-dark w-full !pl-9"
              />
            </div>
            <Button type="submit" variant="secondary">查找</Button>
            <Button type="button" variant="ghost" aria-label="刷新" title="刷新" onClick={() => query.refetch()}>
              <IconRefresh size={15} />
            </Button>
          </form>
        </div>
      </Card>

      {query.isLoading ? (
        <div className="flex h-72 items-center justify-center"><LoadingSpinner size="lg" text="读取分析记录..." /></div>
      ) : !data || data.tasks.length === 0 ? (
        <Card>
          <div className="flex flex-col items-center justify-center py-14 text-center">
            <span className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-accent-blue/10 text-accent-blue ring-1 ring-inset ring-accent-blue/25">
              <IconHistory size={25} />
            </span>
            <div className="text-sm font-semibold text-text-primary">没有找到分析记录</div>
            <p className="mt-2 text-xs text-text-muted">调整筛选条件，或从总览创建一项新的分析。</p>
            <Button className="mt-5" onClick={() => navigate('/')}>去分析</Button>
          </div>
        </Card>
      ) : (
        <Card title={`分析存档 · ${data.total} 条`} noPadding>
          <div className="divide-y divide-border/40">
            {data.tasks.map((task) => {
              const active = task.status === 'running' || task.status === 'queued';
              const description = task.status === 'failed'
                ? task.error || task.message
                : task.report_excerpt || task.message;
              return (
                <button
                  key={task.task_id}
                  type="button"
                  onClick={() => navigate(`/research/${task.task_id}`)}
                  className="group flex w-full gap-4 px-5 py-4 text-left transition-colors hover:bg-bg-hover/35"
                >
                  <StatusIcon task={task} />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-text-primary group-hover:text-accent-gold">{task.ts_code}</span>
                      <Badge variant={statusVariant(task.status)} dot>{STATUS_LABELS[task.status]}</Badge>
                      <Badge variant="info">{task.target_type === 'theme' ? '主题研究' : '个股研究'}</Badge>
                      {task.partial_result && <Badge variant="warning">部分结果汇总</Badge>}
                    </div>
                    <p className="mt-1.5 line-clamp-2 text-xs leading-5 text-text-muted">{description || '暂无摘要'}</p>
                    {active && (
                      <div className="mt-2 h-1 max-w-md overflow-hidden rounded-full bg-bg-primary/80">
                        <div className="h-full rounded-full bg-gradient-to-r from-accent-gold to-accent-orange" style={{ width: `${task.progress}%` }} />
                      </div>
                    )}
                    <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-text-muted/80">
                      <span className="flex items-center gap-1"><IconClock size={12} />{formatDate(task.completed_at || task.created_at)}</span>
                      <span>研究模块 {task.agent_count}/{task.expected_agent_count}</span>
                      <span className="font-mono">{task.task_id.slice(0, 8)}</span>
                    </div>
                  </div>
                  <IconArrowRight size={17} className="mt-3 shrink-0 text-text-muted transition-transform group-hover:translate-x-1 group-hover:text-accent-gold" />
                </button>
              );
            })}
          </div>
          <div className="flex items-center justify-between border-t border-border/40 px-5 py-3">
            <span className="text-xs text-text-muted">第 {page} / {totalPages} 页</span>
            <div className="flex gap-2">
              <Button size="sm" variant="secondary" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</Button>
              <Button size="sm" variant="secondary" disabled={page >= totalPages} onClick={() => setPage((value) => value + 1)}>下一页</Button>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
