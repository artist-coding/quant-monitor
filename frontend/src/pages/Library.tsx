import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { fetchLibrary, type LibraryItem, type LibraryListResponse } from '../api/library';
import ApiErrorState from '../components/ui/ApiErrorState';
import Badge from '../components/ui/Badge';
import Button from '../components/ui/Button';
import Card from '../components/ui/Card';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import PageHeader from '../components/ui/PageHeader';
import { IconBook, IconClock, IconExternal, IconRefresh, IconSearch } from '../components/ui/icons';

// 分组键：'all' | root | `${root}/${category}`
const ALL = 'all';

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

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

interface GroupOption {
  value: string;
  label: string;
  count: number;
}

function buildGroups(data: LibraryListResponse | undefined): GroupOption[] {
  if (!data) return [];
  const options: GroupOption[] = [{ value: ALL, label: '全部', count: data.total }];
  for (const root of data.roots) {
    if (root.count === 0) continue;
    options.push({ value: root.key, label: root.label, count: root.count });
    const categories = new Map<string, number>();
    for (const item of data.items) {
      if (item.root === root.key && item.category) {
        categories.set(item.category, (categories.get(item.category) ?? 0) + 1);
      }
    }
    for (const [category, count] of categories) {
      options.push({ value: `${root.key}/${category}`, label: `${root.label} · ${category}`, count });
    }
  }
  return options;
}

function matchesGroup(item: LibraryItem, group: string): boolean {
  if (group === ALL) return true;
  if (group === item.root) return true;
  return group === `${item.root}/${item.category}`;
}

function EmptyLibrary({ data, onRefresh }: { data: LibraryListResponse; onRefresh: () => void }) {
  return (
    <Card>
      <div className="flex flex-col items-center justify-center py-14 text-center">
        <span className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-accent-blue/10 text-accent-blue ring-1 ring-inset ring-accent-blue/25">
          <IconBook size={25} />
        </span>
        <div className="text-sm font-semibold text-text-primary">资料库还是空的</div>
        <p className="mt-2 max-w-md text-xs leading-5 text-text-muted">
          把 .html 文件放进下面任一目录，刷新即可出现，不需要重新构建前端。子目录名会成为分类，
          研报引用的图片、CSS 放在旁边用相对路径。
        </p>
        <div className="mt-5 w-full max-w-md space-y-2 text-left">
          {data.roots.map((root) => (
            <div key={root.key} className="flex items-center justify-between rounded-lg border border-border/60 bg-bg-primary/30 px-3 py-2 text-xs">
              <span className="flex items-center gap-2">
                <Badge variant={root.exists ? 'info' : 'warning'}>{root.label}</Badge>
                <span className="font-mono text-text-secondary">{root.path}</span>
              </span>
              <span className="text-text-muted">{root.exists ? '目录存在' : '目录尚未创建'}</span>
            </div>
          ))}
        </div>
        <Button className="mt-5" variant="secondary" onClick={onRefresh}>
          <IconRefresh size={14} />
          刷新
        </Button>
      </div>
    </Card>
  );
}

export default function Library() {
  const [params, setParams] = useSearchParams();
  const selectedId = params.get('f') || '';
  const [keyword, setKeyword] = useState('');
  const [group, setGroup] = useState(ALL);
  const [frameKey, setFrameKey] = useState(0);

  const query = useQuery({ queryKey: ['library'], queryFn: fetchLibrary });
  const data = query.data;

  const groups = useMemo(() => buildGroups(data), [data]);
  const filtered = useMemo(() => {
    if (!data) return [];
    const needle = keyword.trim().toLowerCase();
    return data.items.filter((item) => {
      if (!matchesGroup(item, group)) return false;
      if (!needle) return true;
      return `${item.title} ${item.rel_path} ${item.description}`.toLowerCase().includes(needle);
    });
  }, [data, keyword, group]);

  // URL 里没选（首次进入）或文件已被删时，默认展示最新的一份；只有点击才写 URL
  const selected = useMemo(
    () => data?.items.find((item) => item.id === selectedId) ?? data?.items[0] ?? null,
    [data, selectedId],
  );

  if (query.isError) {
    return <ApiErrorState message={(query.error as Error)?.message || '读取资料库失败'} onRetry={() => query.refetch()} />;
  }

  return (
    <div className="space-y-5 animate-fade-up">
      <PageHeader
        title="资料库"
        description={`方案文档与自写研报，HTML 原样展示${data ? ` · 共 ${data.total} 份` : ''}`}
        actions={
          <Button variant="ghost" aria-label="刷新列表" title="刷新列表" onClick={() => query.refetch()}>
            <IconRefresh size={15} />
            刷新列表
          </Button>
        }
      />

      {query.isLoading ? (
        <div className="flex h-72 items-center justify-center"><LoadingSpinner size="lg" text="读取资料库..." /></div>
      ) : !data || data.total === 0 ? (
        data ? <EmptyLibrary data={data} onRefresh={() => query.refetch()} /> : null
      ) : (
        <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
          {/* 左：筛选 + 列表 */}
          <Card noPadding className="self-start lg:sticky lg:top-[4.5rem]">
            <div className="space-y-3 border-b border-border/40 p-3">
              <div className="relative">
                <IconSearch size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
                <input
                  value={keyword}
                  onChange={(event) => setKeyword(event.target.value)}
                  placeholder="搜索标题或文件名"
                  className="input-dark w-full !pl-9"
                />
              </div>
              <div className="flex flex-wrap gap-1.5">
                {groups.map((option) => {
                  const active = group === option.value;
                  return (
                    <button
                      key={option.value}
                      type="button"
                      onClick={() => setGroup(option.value)}
                      className={`rounded-lg border px-2.5 py-1 text-[11px] font-medium transition-colors ${active ? 'border-accent-gold/40 bg-accent-gold/10 text-accent-gold' : 'border-border/60 bg-bg-primary/30 text-text-muted hover:border-border hover:text-text-primary'}`}
                    >
                      {option.label} {option.count}
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="max-h-[calc(100vh-17rem)] divide-y divide-border/40 overflow-y-auto">
              {filtered.length === 0 ? (
                <div className="px-4 py-10 text-center text-xs text-text-muted">没有匹配的文件</div>
              ) : filtered.map((item) => {
                const active = item.id === selected?.id;
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setParams({ f: item.id })}
                    className={`group flex w-full flex-col gap-1.5 px-4 py-3 text-left transition-colors ${active ? 'bg-accent-gold/[0.08]' : 'hover:bg-bg-hover/35'}`}
                  >
                    <span className={`line-clamp-2 text-sm font-semibold leading-5 ${active ? 'text-accent-gold' : 'text-text-primary group-hover:text-accent-gold'}`}>
                      {item.title}
                    </span>
                    {item.description && (
                      <span className="line-clamp-2 text-xs leading-5 text-text-muted">{item.description}</span>
                    )}
                    <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-text-muted/80">
                      <Badge variant="info">{item.category ? `${item.root_label} · ${item.category}` : item.root_label}</Badge>
                      <span className="flex items-center gap-1"><IconClock size={12} />{formatDate(item.modified_at)}</span>
                      <span className="font-mono">{formatSize(item.size)}</span>
                    </span>
                  </button>
                );
              })}
            </div>
          </Card>

          {/* 右：iframe 原样展示 */}
          <Card noPadding className="min-w-0 overflow-hidden">
            {selected ? (
              <>
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/40 px-4 py-2.5">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold text-text-primary">{selected.title}</div>
                    <div className="truncate font-mono text-[11px] text-text-muted">{selected.root_label} / {selected.rel_path}</div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <Button size="sm" variant="ghost" title="重新加载页面" onClick={() => setFrameKey((value) => value + 1)}>
                      <IconRefresh size={14} />
                      重新加载
                    </Button>
                    <a
                      href={selected.url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 rounded-md border border-border/70 bg-bg-hover/60 px-2.5 py-1 text-xs font-medium text-text-secondary transition-all hover:border-border hover:bg-bg-hover hover:text-text-primary"
                    >
                      <IconExternal size={14} />
                      新标签打开
                    </a>
                  </div>
                </div>
                {/* 不给 allow-same-origin：研报里的脚本可以跑，但拿不到看板自己的存储 */}
                <iframe
                  key={`${selected.id}-${frameKey}`}
                  src={selected.url}
                  title={selected.title}
                  sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms allow-modals allow-downloads"
                  className="block w-full bg-white"
                  style={{ height: 'calc(100vh - 13.5rem)', minHeight: 520 }}
                />
              </>
            ) : (
              <div className="flex h-72 items-center justify-center text-xs text-text-muted">从左侧选择一份文件</div>
            )}
          </Card>
        </div>
      )}
    </div>
  );
}
