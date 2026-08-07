import { Fragment, type ReactNode } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { fetchResearch } from '../api/research';
import Card from '../components/ui/Card';
import Badge from '../components/ui/Badge';
import Button from '../components/ui/Button';
import PageHeader from '../components/ui/PageHeader';
import ApiErrorState from '../components/ui/ApiErrorState';
import { IconAlert, IconCheck, IconClock, IconHistory, IconRadar, IconRefresh } from '../components/ui/icons';

function renderInline(text: string): ReactNode[] {
  const pattern = /(\*\*[^*]+\*\*|\[[^\]]+\]\(https?:\/\/[^)]+\)|https?:\/\/[^\s)]+)/g;
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text))) {
    if (match.index > cursor) nodes.push(text.slice(cursor, match.index));
    const token = match[0];
    const link = /^\[([^\]]+)\]\((https?:\/\/[^)]+)\)$/.exec(token);
    if (token.startsWith('**')) {
      nodes.push(<strong key={match.index} className="font-semibold text-text-primary">{token.slice(2, -2)}</strong>);
    } else if (link) {
      nodes.push(<a key={match.index} href={link[2]} target="_blank" rel="noreferrer" className="text-accent-blue hover:text-accent-gold underline underline-offset-2">{link[1]}</a>);
    } else {
      nodes.push(<a key={match.index} href={token} target="_blank" rel="noreferrer" className="text-accent-blue hover:text-accent-gold underline underline-offset-2 break-all">{token}</a>);
    }
    cursor = match.index + token.length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

type ReportBlock =
  | { type: 'line'; key: number; line: string }
  | { type: 'table'; key: number; headers: string[]; rows: string[][] };

function parseTableRow(line: string): string[] {
  const source = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  const cells: string[] = [];
  let cell = '';
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (char === '\\' && source[index + 1] === '|') {
      cell += '|';
      index += 1;
    } else if (char === '|') {
      cells.push(cell.trim());
      cell = '';
    } else {
      cell += char;
    }
  }
  cells.push(cell.trim());
  return cells;
}

function isTableDivider(line: string): boolean {
  if (!line.trim().startsWith('|')) return false;
  const cells = parseTableRow(line);
  return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function parseReport(report: string): ReportBlock[] {
  const lines = report.split('\n');
  const blocks: ReportBlock[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index].trim();
    if (line.startsWith('|')) {
      let dividerIndex = index + 1;
      while (dividerIndex < lines.length && !lines[dividerIndex].trim()) dividerIndex += 1;
      if (dividerIndex < lines.length && isTableDivider(lines[dividerIndex])) {
        const headers = parseTableRow(line);
        const rows: string[][] = [];
        let cursor = dividerIndex + 1;
        while (cursor < lines.length) {
          let rowIndex = cursor;
          while (rowIndex < lines.length && !lines[rowIndex].trim()) rowIndex += 1;
          const rowLine = lines[rowIndex]?.trim() || '';
          if (!rowLine.startsWith('|') || isTableDivider(rowLine)) break;
          const cells = parseTableRow(rowLine);
          rows.push(headers.map((_, cellIndex) => cells[cellIndex] || ''));
          cursor = rowIndex + 1;
        }
        blocks.push({ type: 'table', key: index, headers, rows });
        index = cursor;
        continue;
      }
    }
    blocks.push({ type: 'line', key: index, line: lines[index] });
    index += 1;
  }
  return blocks;
}

function renderReportLine(rawLine: string, key: number): ReactNode {
  const line = rawLine.trim();
  if (!line) return <div key={key} className="h-2" />;
  const heading = /^(#{1,4})\s+(.+)$/.exec(line);
  if (heading) {
    const classes = heading[1].length <= 2
      ? 'mt-8 mb-3 text-xl font-bold text-accent-gold'
      : 'mt-6 mb-2 text-base font-semibold text-text-primary';
    return <h2 key={key} className={classes}>{renderInline(heading[2])}</h2>;
  }
  if (/^[-*_]{3,}$/.test(line)) return <hr key={key} className="my-5 border-border/60" />;
  const bullet = /^[-*]\s+(.+)$/.exec(line);
  if (bullet) return <div key={key} className="flex gap-3 pl-2"><span className="mt-3 h-1.5 w-1.5 shrink-0 rounded-full bg-accent-gold" /><p>{renderInline(bullet[1])}</p></div>;
  const ordered = /^(\d+)\.\s+(.+)$/.exec(line);
  if (ordered) return <div key={key} className="flex gap-3 pl-1"><span className="min-w-6 font-mono text-accent-gold">{ordered[1]}.</span><p>{renderInline(ordered[2])}</p></div>;
  if (line.startsWith('>')) return <blockquote key={key} className="my-3 border-l-2 border-accent-gold/60 bg-accent-gold/[0.05] px-4 py-2 text-text-primary">{renderInline(line.slice(1).trim())}</blockquote>;
  return <p key={key}>{renderInline(line)}</p>;
}

function ResearchReport({ report }: { report: string }) {
  const blocks = parseReport(report);
  return (
    <article className="space-y-2 text-[15px] leading-8 text-text-secondary">
      {blocks.map((block) => block.type === 'line'
        ? renderReportLine(block.line, block.key)
        : (
          <div key={block.key} className="my-5 overflow-x-auto rounded-xl border border-border/70 bg-bg-primary/35">
            <table className="min-w-full border-collapse text-left text-[13px] leading-6">
              <thead className="bg-accent-gold/[0.08]">
                <tr>
                  {block.headers.map((header, index) => (
                    <th key={`${block.key}-h-${index}`} className="whitespace-nowrap border-b border-border/70 px-4 py-2.5 font-semibold text-accent-gold">
                      {renderInline(header)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border/40">
                {block.rows.map((row, rowIndex) => (
                  <tr key={`${block.key}-r-${rowIndex}`} className="align-top transition-colors hover:bg-bg-hover/30">
                    {row.map((cell, cellIndex) => (
                      <td key={`${block.key}-r-${rowIndex}-c-${cellIndex}`} className={`px-4 py-3 text-text-secondary ${cellIndex < 2 ? 'whitespace-nowrap font-medium text-text-primary' : 'min-w-64'}`}>
                        {renderInline(cell)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
    </article>
  );
}

export default function Research() {
  const { taskId = '' } = useParams();
  const navigate = useNavigate();
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['research', taskId],
    queryFn: () => fetchResearch(taskId),
    enabled: Boolean(taskId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === 'queued' || status === 'running' ? 2500 : false;
    },
  });

  if (isError) {
    return <ApiErrorState message={(error as Error)?.message || '读取调研任务失败'} onRetry={() => refetch()} />;
  }

  const running = !data || data.status === 'queued' || data.status === 'running';
  const statusVariant = data?.status === 'completed' ? 'success' : data?.status === 'failed' ? 'danger' : 'warning';

  return (
    <div className="space-y-5 animate-fade-up">
      <PageHeader title="股票分析" description={data ? `${data.ts_code} · AI 多维深度调研` : '正在创建分析任务'} />

      <Card highlight>
        <div className="flex flex-col gap-5 md:flex-row md:items-center">
          <div className={`flex h-14 w-14 shrink-0 items-center justify-center rounded-2xl ring-1 ring-inset ${running ? 'bg-accent-gold/10 text-accent-gold ring-accent-gold/30 animate-soft-pulse' : data?.status === 'completed' ? 'bg-accent-green/10 text-accent-green ring-accent-green/30' : 'bg-accent-red/10 text-accent-red ring-accent-red/30'}`}>
            {running ? <IconRadar size={27} /> : data?.status === 'completed' ? <IconCheck size={27} /> : <IconAlert size={27} />}
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-lg font-bold text-text-primary">{data?.ts_code || '--'}</span>
              {data && <Badge variant={statusVariant} dot>{data.status.toUpperCase()}</Badge>}
              <Badge variant="info">多智能体协作</Badge>
              <Badge variant="default">Z哥投资框架</Badge>
              {data?.partial_result && <Badge variant="warning">部分结果汇总</Badge>}
            </div>
            <div className="mt-2 flex items-center gap-2 text-sm text-text-muted">
              <IconClock size={14} />
              <span>{isLoading ? '正在连接后端...' : data?.message}</span>
              {running && Boolean(data?.expected_agent_count) && (
                <span className="font-mono text-accent-gold">
                  {data?.agent_count || 0}/{data?.expected_agent_count}
                </span>
              )}
            </div>
            <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-bg-primary/80">
              <div className="h-full rounded-full bg-gradient-to-r from-accent-gold to-accent-orange transition-all duration-700" style={{ width: `${data?.progress || 8}%` }} />
            </div>
          </div>
          <div className="flex shrink-0 gap-2">
            <Button variant="ghost" onClick={() => navigate('/research')}>
              <IconHistory size={14} /> 分析记录
            </Button>
            {!running && (
              <Button variant="secondary" onClick={() => navigate('/')}>
                <IconRefresh size={14} /> 再次调研
              </Button>
            )}
          </div>
        </div>
      </Card>

      {running && (
        <Card title="调研进行中">
          <div className="grid gap-3 text-sm text-text-secondary md:grid-cols-3">
            {['基本面与行业并行检索', '公告与最新事件交叉验证', '技术结构与 Z 哥战法汇总'].map((item, index) => (
              <div key={item} className="flex items-center gap-3 rounded-lg border border-border/50 bg-bg-primary/30 px-4 py-3">
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-accent-gold/10 font-mono text-xs text-accent-gold">{index + 1}</span>
                {item}
              </div>
            ))}
          </div>
          <p className="mt-4 text-xs text-text-muted">深度分析通常需要数分钟；可以离开本页，任务会继续在服务器运行。</p>
        </Card>
      )}

      {data?.status === 'failed' && (
        <Card title="调研未完成">
          <p className="text-sm leading-7 text-accent-red">{data.error || 'Kimi CLI 调研失败，请检查服务器配置。'}</p>
        </Card>
      )}

      {data?.status === 'completed' && data.report && (
        <Fragment>
          {data.partial_result && (
            <div className="rounded-lg border border-accent-orange/30 bg-accent-orange/[0.06] px-4 py-3 text-sm leading-6 text-text-secondary">
              本报告基于 {data.agent_count}/{data.expected_agent_count} 份完整研究结果汇总；未完成的研究单元未纳入结论。
            </div>
          )}
          <Card title="AI 深度分析报告">
            <ResearchReport report={data.report} />
          </Card>
          <div className="rounded-lg border border-accent-gold/20 bg-accent-gold/[0.05] px-4 py-3 text-xs leading-6 text-text-muted">
            本报告基于公开信息和历史分析框架生成，不构成投资建议。投资者应自主决策，并自行承担盈亏。
          </div>
        </Fragment>
      )}
    </div>
  );
}
