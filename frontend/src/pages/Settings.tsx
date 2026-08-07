import { useQuery } from '@tanstack/react-query';
import api from '../api/client';
import Card from '../components/ui/Card';
import Badge from '../components/ui/Badge';
import PageHeader from '../components/ui/PageHeader';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import ApiErrorState from '../components/ui/ApiErrorState';
import { IconDatabase } from '../components/ui/icons';

export default function Settings() {
  const { data: health, isLoading: loadingHealth, isError: healthError, error: healthErr, refetch: refetchHealth } = useQuery({
    queryKey: ['health'],
    queryFn: async () => {
      const { data } = await api.get('/system/health');
      return data;
    },
  });

  const { data: syncStatus, isLoading: loadingSync, isError: syncError, error: syncErr, refetch: refetchSync } = useQuery({
    queryKey: ['sync-status'],
    queryFn: async () => {
      const { data } = await api.get('/system/sync/status');
      return data;
    },
  });

  if (loadingHealth || loadingSync) {
    return <div className="flex items-center justify-center h-96"><LoadingSpinner size="lg" /></div>;
  }

  if (healthError || syncError) {
    const err = (healthErr || syncErr) as Error | null;
    return (
      <ApiErrorState
        message={err?.message || '加载系统状态失败'}
        onRetry={() => { refetchHealth(); refetchSync(); }}
      />
    );
  }

  const healthRows: Array<{ label: string; node: React.ReactNode }> = [
    { label: '状态', node: <Badge variant="success" dot>{health?.status || 'unknown'}</Badge> },
    { label: '数据模式', node: <span className="text-text-primary font-medium">{health?.data_mode || '--'}</span> },
    {
      label: '数据库',
      node: (
        <Badge variant={health?.db_exists ? 'success' : 'danger'} dot>
          {health?.db_exists ? '已连接' : '未找到'}
        </Badge>
      ),
    },
    { label: 'API 版本', node: <span className="text-text-primary font-mono">{health?.version || '--'}</span> },
  ];

  return (
    <div className="space-y-5 animate-fade-up">
      <PageHeader title="系统设置" description="服务健康状态与数据同步记录" />

      {/* System Info */}
      <Card title="系统状态">
        <div className="divide-y divide-border/30">
          {healthRows.map((row) => (
            <div key={row.label} className="flex items-center justify-between py-3 first:pt-0 last:pb-0 text-sm">
              <span className="flex items-center gap-2 text-text-muted">
                <IconDatabase size={14} className="text-text-muted/60" />
                {row.label}
              </span>
              {row.node}
            </div>
          ))}
        </div>
      </Card>

      {/* Sync Status */}
      <Card title="数据同步记录">
        {!syncStatus || syncStatus.logs.length === 0 ? (
          <div className="text-center py-10 text-sm text-text-muted">暂无同步记录</div>
        ) : (
          <div className="overflow-x-auto max-h-96 overflow-y-auto -mx-5 -mb-5">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-bg-elevated z-10">
                <tr className="border-b border-border/60">
                  <th className="text-left px-5 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">类型</th>
                  <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">代码</th>
                  <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">最后日期</th>
                  <th className="text-left px-3 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">状态</th>
                  <th className="text-left px-5 py-2.5 text-[11px] font-medium uppercase tracking-wider text-text-muted">消息</th>
                </tr>
              </thead>
              <tbody>
                {syncStatus.logs.map((log: Record<string, string>, i: number) => (
                  <tr key={i} className="border-b border-border/30 last:border-0 transition-colors hover:bg-bg-hover/30">
                    <td className="px-5 py-2.5 text-text-secondary">{log.data_type}</td>
                    <td className="px-3 py-2.5 font-mono text-accent-gold">{log.ts_code || '--'}</td>
                    <td className="px-3 py-2.5 text-text-secondary font-mono">{log.last_date || '--'}</td>
                    <td className="px-3 py-2.5">
                      <Badge variant={log.status === 'success' ? 'success' : 'danger'} dot>
                        {log.status}
                      </Badge>
                    </td>
                    <td className="px-5 py-2.5 text-text-muted max-w-64 truncate">{log.message || '--'}</td>
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
