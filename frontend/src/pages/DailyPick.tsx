import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import {
  addAmv,
  createReview,
  createScan,
  fetchAmv,
  fetchLatestScan,
  fetchScan,
  fetchThemeRanking,
  fetchThemes,
  removeTheme,
  setThemeMembers,
  upsertTheme,
  type ScanTask,
  type Theme,
} from '../api/daily';
import Card from '../components/ui/Card';
import Button from '../components/ui/Button';
import Badge from '../components/ui/Badge';
import PageHeader from '../components/ui/PageHeader';
import LoadingSpinner from '../components/ui/LoadingSpinner';
import { IconAlert, IconCheck, IconRadar, IconZap } from '../components/ui/icons';

/** 扫描要跑 2~3 分钟，运行中每 3 秒拉一次进度 */
const SCAN_POLL_MS = 3000;

function todayCompact(): string {
  const d = new Date();
  return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`;
}

// ==================== 活跃市值：总开关 ====================

function AmvPanel() {
  const qc = useQueryClient();
  const [date, setDate] = useState(todayCompact());
  const [close, setClose] = useState('');
  const [pct, setPct] = useState('');
  const [err, setErr] = useState('');

  const { data: amv, isLoading } = useQuery({ queryKey: ['amv'], queryFn: () => fetchAmv() });

  const save = useMutation({
    mutationFn: addAmv,
    onSuccess: () => {
      setErr('');
      setClose('');
      setPct('');
      qc.invalidateQueries({ queryKey: ['amv'] });
    },
    onError: (e: Error) => setErr(e.message),
  });

  const submit = () => {
    const c = close.trim() ? Number(close) : undefined;
    const p = pct.trim() ? Number(pct) : undefined;
    if (c === undefined && p === undefined) {
      setErr('收盘价与涨幅至少填一个；优先填收盘价');
      return;
    }
    save.mutate({ trade_date: date.trim(), close: c, pct_chg: p });
  };

  if (isLoading) return <Card title="活跃市值"><LoadingSpinner text="读取中..." /></Card>;

  const bull = amv?.can_select;
  return (
    <Card title="活跃市值 · 选股总开关">
      {amv?.available ? (
        <div className="mb-4 flex flex-wrap items-center gap-3">
          <Badge variant={bull ? 'success' : 'danger'} dot>
            {amv.regime}
          </Badge>
          <span className="text-sm text-text-secondary">{amv.trade_date}</span>
          <span className="text-sm text-text-primary tabular-nums">
            收盘 {amv.close.toLocaleString(undefined, { maximumFractionDigits: 2 })}
          </span>
          {amv.pct_chg !== null && (
            <span className={`text-sm tabular-nums ${amv.pct_chg >= 0 ? 'text-up' : 'text-down'}`}>
              {amv.pct_chg >= 0 ? '+' : ''}
              {amv.pct_chg.toFixed(2)}%
            </span>
          )}
          <span className={`text-sm font-medium ${bull ? 'text-accent-green' : 'text-accent-red'}`}>
            {bull ? '→ 可以选股 / 可新建仓' : '→ 停止选股'}
          </span>
        </div>
      ) : (
        <div className="mb-4 rounded-lg bg-accent-gold/10 px-3 py-2 text-sm text-accent-gold">
          还没有活跃市值数据。它是选股的总开关，没有它扫描不会执行。
          先用 <code className="font-mono">zt amv import</code> 导入历史，再在这里逐日录入。
        </div>
      )}

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-text-muted">交易日</span>
          <input
            value={date}
            onChange={(e) => setDate(e.target.value)}
            placeholder="20260810"
            className="w-32 rounded-lg bg-bg-hover/40 px-3 py-2 text-sm text-text-primary ring-1 ring-inset ring-border/60 outline-none focus:ring-accent-gold/50"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-text-muted">收盘价（首选）</span>
          <input
            value={close}
            onChange={(e) => setClose(e.target.value)}
            placeholder="213847.41"
            inputMode="decimal"
            className="w-36 rounded-lg bg-bg-hover/40 px-3 py-2 text-sm text-text-primary ring-1 ring-inset ring-border/60 outline-none focus:ring-accent-gold/50"
          />
        </label>
        <span className="pb-2 text-xs text-text-muted">或</span>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-text-muted">日涨幅 %</span>
          <input
            value={pct}
            onChange={(e) => setPct(e.target.value)}
            placeholder="2.46"
            inputMode="decimal"
            className="w-28 rounded-lg bg-bg-hover/40 px-3 py-2 text-sm text-text-primary ring-1 ring-inset ring-border/60 outline-none focus:ring-accent-gold/50"
          />
        </label>
        <Button onClick={submit} disabled={save.isPending}>
          {save.isPending ? '保存中…' : '录入'}
        </Button>
      </div>

      <p className="mt-3 text-xs text-text-muted leading-relaxed">
        规则：单日跌幅 &lt; {amv?.bear_threshold ?? -2.3}% → 空头区间（停止选股）；
        单日或连续两日累计涨幅 ≥ {amv?.bull_threshold ?? 4}% → 多头区间；否则沿用前一日。
        <br />
        <span className="text-accent-gold/80">
          尽量填收盘价：涨幅若是四舍五入到两位小数的值，在 −2.3% 边界附近会判出相反的区间
          （−2.295% 与 −2.303% 都显示 −2.30%，一个多头一个空头）。
        </span>
      </p>

      {(err || save.data?.precision_warning) && (
        <div className={`mt-3 rounded-lg px-3 py-2 text-sm ${err ? 'bg-accent-red/10 text-accent-red' : 'bg-accent-gold/10 text-accent-gold'}`}>
          {err || save.data?.precision_warning}
        </div>
      )}

      {!!amv?.segments?.length && (
        <div className="mt-4 border-t border-border/40 pt-3">
          <div className="mb-2 text-xs text-text-muted">最近区间</div>
          <div className="flex flex-wrap gap-2">
            {amv.segments.map((s) => (
              <span
                key={`${s.regime}-${s.start}`}
                className={`rounded-md px-2 py-1 text-xs tabular-nums ring-1 ring-inset ${
                  s.regime === '多头区间'
                    ? 'bg-accent-green/10 text-accent-green ring-accent-green/25'
                    : 'bg-accent-red/10 text-accent-red ring-accent-red/25'
                }`}
              >
                {s.start}~{s.end} · {s.days}日
              </span>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

// ==================== 主线维护 ====================

function ThemePanel() {
  const qc = useQueryClient();
  const [name, setName] = useState('');
  const [desc, setDesc] = useState('');
  const [editing, setEditing] = useState<string | null>(null);
  const [codes, setCodes] = useState('');
  const [err, setErr] = useState('');

  const { data: themes = [], isLoading } = useQuery({ queryKey: ['themes'], queryFn: fetchThemes });
  const { data: ranking } = useQuery({ queryKey: ['themeRanking'], queryFn: () => fetchThemeRanking(5) });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['themes'] });
    qc.invalidateQueries({ queryKey: ['themeRanking'] });
  };

  const add = useMutation({
    mutationFn: upsertTheme,
    onSuccess: () => {
      setName('');
      setDesc('');
      setErr('');
      invalidate();
    },
    onError: (e: Error) => setErr(e.message),
  });
  const del = useMutation({ mutationFn: removeTheme, onSuccess: invalidate });
  const saveMembers = useMutation({
    mutationFn: ({ theme, list }: { theme: string; list: string[] }) => setThemeMembers(theme, list),
    onSuccess: () => {
      setEditing(null);
      setCodes('');
      invalidate();
    },
    onError: (e: Error) => setErr(e.message),
  });

  const strengthOf = useMemo(() => {
    const map = new Map<string, number>();
    ranking?.themes.forEach((t) => map.set(t.theme, t.strength));
    return map;
  }, [ranking]);

  return (
    <Card title="主线板块 · 人工维护">
      <p className="mb-4 text-xs text-text-muted leading-relaxed">
        系统<span className="text-text-secondary">不判断</span>当前在炒什么，也不判断某只票属不属于某条主线
        ——本地没有题材数据源，硬猜只会产出看着合理实则编造的归类。
        主线清单和成员由你给定，系统只负责用行情数据把它们的<span className="text-text-secondary">强弱排出顺序</span>。
      </p>

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-text-muted">主线名称</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="商业航天"
            className="w-36 rounded-lg bg-bg-hover/40 px-3 py-2 text-sm text-text-primary ring-1 ring-inset ring-border/60 outline-none focus:ring-accent-gold/50"
          />
        </label>
        <label className="flex flex-1 flex-col gap-1 min-w-[200px]">
          <span className="text-xs text-text-muted">说明（给 Kimi 复核时看的口径）</span>
          <input
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            placeholder="卫星互联网 / 火箭发射产业链"
            className="w-full rounded-lg bg-bg-hover/40 px-3 py-2 text-sm text-text-primary ring-1 ring-inset ring-border/60 outline-none focus:ring-accent-gold/50"
          />
        </label>
        <Button onClick={() => name.trim() && add.mutate({ name: name.trim(), description: desc.trim() })}>
          添加主线
        </Button>
      </div>

      {err && <div className="mb-3 rounded-lg bg-accent-red/10 px-3 py-2 text-sm text-accent-red">{err}</div>}

      {isLoading ? (
        <LoadingSpinner text="读取中..." />
      ) : themes.length === 0 ? (
        <div className="rounded-lg bg-bg-hover/30 px-3 py-4 text-center text-sm text-text-muted">
          还没有主线。未导入主线时，选股的第二阶段会退回 Tushare 行业分类兜底
          （口径粗，"元器件"里既有光模块也有电阻厂），筛选门槛还会额外加严 10 分。
        </div>
      ) : (
        <div className="space-y-2">
          {themes.map((t: Theme) => (
            <div key={t.name} className="rounded-lg border border-border/50 bg-bg-hover/20 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-text-primary">{t.name}</span>
                {strengthOf.has(t.name) && (
                  <Badge variant={strengthOf.get(t.name)! >= 50 ? 'success' : 'default'}>
                    强度 {strengthOf.get(t.name)!.toFixed(1)}
                  </Badge>
                )}
                <span className="text-xs text-text-muted">{t.member_count} 只成员</span>
                <span className="flex-1 truncate text-xs text-text-muted">{t.description}</span>
                <button
                  onClick={() => {
                    setEditing(editing === t.name ? null : t.name);
                    setCodes('');
                  }}
                  className="text-xs text-accent-blue hover:underline"
                >
                  {editing === t.name ? '收起' : '编辑成员'}
                </button>
                <button onClick={() => del.mutate(t.name)} className="text-xs text-accent-red hover:underline">
                  删除
                </button>
              </div>
              {editing === t.name && (
                <div className="mt-3 space-y-2">
                  <textarea
                    value={codes}
                    onChange={(e) => setCodes(e.target.value)}
                    rows={3}
                    placeholder="600879.SH, 002151.SZ, 300308.SZ（逗号/空格/换行分隔，整体替换现有成员）"
                    className="w-full rounded-lg bg-bg-hover/40 px-3 py-2 font-mono text-xs text-text-primary ring-1 ring-inset ring-border/60 outline-none focus:ring-accent-gold/50"
                  />
                  <div className="flex items-center gap-2">
                    <Button
                      onClick={() =>
                        saveMembers.mutate({
                          theme: t.name,
                          list: codes
                            .split(/[\s,，;；]+/)
                            .map((c) => c.trim().toUpperCase())
                            .filter(Boolean),
                        })
                      }
                      disabled={saveMembers.isPending}
                    >
                      保存成员
                    </Button>
                    <span className="text-xs text-text-muted">
                      成员少于 3 只的主线不参与强度排名（统计不可信），扫描时会明确提示
                    </span>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {!!ranking?.industries?.length && (
        <div className="mt-4 border-t border-border/40 pt-3">
          <div className="mb-2 text-xs text-text-muted">
            行业强度参照系（{ranking.trade_date}，窗口 {ranking.lookback} 日）— 主线强度以它为标尺定标
          </div>
          <div className="flex flex-wrap gap-1.5">
            {ranking.industries.slice(0, 12).map((i) => (
              <span
                key={i.theme}
                className="rounded-md bg-bg-hover/40 px-2 py-0.5 text-xs text-text-secondary tabular-nums"
              >
                {i.theme} {i.strength.toFixed(0)}
              </span>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

// ==================== 扫描 + Kimi 复核 ====================

function ScanPanel() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [scanId, setScanId] = useState<string | null>(null);
  const [topN, setTopN] = useState(5);
  const [minStrength, setMinStrength] = useState(50);
  const [err, setErr] = useState('');

  const { data: latest } = useQuery({ queryKey: ['scanLatest'], queryFn: fetchLatestScan });

  // 首次进入时接回最近一次扫描，刷新页面不至于丢上下文
  useEffect(() => {
    if (!scanId && latest?.scan_id) setScanId(latest.scan_id);
  }, [latest, scanId]);

  const { data: scan } = useQuery<ScanTask>({
    queryKey: ['scan', scanId],
    queryFn: () => fetchScan(scanId as string),
    enabled: !!scanId,
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === 'queued' || s === 'running' ? SCAN_POLL_MS : false;
    },
  });

  const start = useMutation({
    mutationFn: () =>
      createScan({ top_n: topN, min_group_strength: minStrength, market_gate: 'on', save: true }),
    onSuccess: (t) => {
      setErr('');
      setScanId(t.scan_id);
      qc.invalidateQueries({ queryKey: ['scanLatest'] });
    },
    onError: (e: Error) => setErr(e.message),
  });

  const review = useMutation({
    mutationFn: () => createReview(scanId as string),
    onSuccess: (task) => navigate(`/research/${task.task_id}`),
    onError: (e: Error) => setErr(e.message),
  });

  const running = scan?.status === 'queued' || scan?.status === 'running';
  const stoppedLabels: Record<string, string> = {
    no_trigger: '最近3日无B1',
    veto: '一票否决',
    excluded: '不可交易(ST/北交所)',
    market_gate: '区间门槛',
    other: '数据不足',
  };

  return (
    <Card title="每日选股">
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-text-muted">最多选几只</span>
          <input
            type="number"
            min={1}
            max={20}
            value={topN}
            onChange={(e) => setTopN(Number(e.target.value))}
            className="w-20 rounded-lg bg-bg-hover/40 px-3 py-2 text-sm text-text-primary ring-1 ring-inset ring-border/60 outline-none focus:ring-accent-gold/50"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-text-muted">主线/行业强度门槛</span>
          <input
            type="number"
            min={0}
            max={100}
            value={minStrength}
            onChange={(e) => setMinStrength(Number(e.target.value))}
            className="w-24 rounded-lg bg-bg-hover/40 px-3 py-2 text-sm text-text-primary ring-1 ring-inset ring-border/60 outline-none focus:ring-accent-gold/50"
          />
        </label>
        <Button onClick={() => start.mutate()} disabled={running || start.isPending}>
          <IconRadar size={15} className="mr-1.5" />
          {running ? '扫描中…' : '开始每日选股'}
        </Button>
        {running && <LoadingSpinner size="sm" text={`${scan?.progress ?? 0}% ${scan?.message ?? ''}`} />}
      </div>

      {err && <div className="mb-3 rounded-lg bg-accent-red/10 px-3 py-2 text-sm text-accent-red">{err}</div>}

      {scan && (
        <>
          {scan.status === 'failed' && (
            <div className="mb-3 rounded-lg bg-accent-red/10 px-3 py-2 text-sm text-accent-red">
              扫描失败：{scan.error}
            </div>
          )}

          {scan.status === 'completed' && (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
                {scan.amv?.regime && (
                  <Badge variant={scan.amv.regime === '多头区间' ? 'success' : 'danger'} dot>
                    {scan.amv.regime}
                  </Badge>
                )}
                {scan.position_hint?.level && (
                  <span className="text-text-secondary">
                    建仓参考：
                    <span className="text-accent-gold">
                      {scan.position_hint.level}（{scan.position_hint.range}）
                    </span>
                  </span>
                )}
                <span className="text-text-muted tabular-nums">
                  扫描 {scan.scanned} 只 · {scan.elapsed}s
                </span>
                {!!scan.counts?.BUY && (
                  <span className="text-text-secondary tabular-nums">
                    BUY {scan.counts.BUY} / WATCH {scan.counts.WATCH ?? 0}
                  </span>
                )}
              </div>

              {!!Object.keys(scan.stopped ?? {}).length && (
                <div className="mb-3 flex flex-wrap gap-1.5 text-xs text-text-muted">
                  淘汰构成：
                  {Object.entries(scan.stopped)
                    .sort((a, b) => b[1] - a[1])
                    .map(([k, v]) => (
                      <span key={k} className="rounded bg-bg-hover/40 px-1.5 py-0.5 tabular-nums">
                        {stoppedLabels[k] ?? k} {v}
                      </span>
                    ))}
                </div>
              )}

              {scan.warnings?.map((w) => (
                <div key={w} className="mb-2 flex gap-2 rounded-lg bg-accent-gold/10 px-3 py-2 text-sm text-accent-gold">
                  <IconAlert size={15} className="mt-0.5 shrink-0" />
                  <span>{w}</span>
                </div>
              ))}

              {scan.blocked ? (
                <div className="rounded-lg bg-accent-red/10 px-3 py-4 text-sm text-accent-red">
                  <div className="font-medium">{scan.blocked}</div>
                  <div className="mt-1 text-text-muted">本次未扫描任何个股。今日不买入是一个合法结论。</div>
                </div>
              ) : scan.picks.length === 0 ? (
                <div className="rounded-lg bg-bg-hover/30 px-3 py-4 text-center text-sm text-text-muted">
                  本日无入选标的——买点与强板块没有交集。不必为了凑数降低门槛。
                </div>
              ) : (
                <>
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="text-left text-xs text-text-muted">
                          <th className="pb-2 pr-3">#</th>
                          <th className="pb-2 pr-3">代码</th>
                          <th className="pb-2 pr-3">名称</th>
                          <th className="pb-2 pr-3 text-right">确认分</th>
                          <th className="pb-2 pr-3">分组</th>
                          <th className="pb-2 pr-3 text-right">组强度</th>
                          <th className="pb-2">触发</th>
                        </tr>
                      </thead>
                      <tbody>
                        {scan.picks.map((p) => (
                          <tr
                            key={p.ts_code}
                            className="cursor-pointer border-t border-border/40 hover:bg-bg-hover/30"
                            onClick={() => navigate(`/stock/${p.ts_code}`)}
                          >
                            <td className="py-2 pr-3 text-accent-gold tabular-nums">{p.rank}</td>
                            <td className="py-2 pr-3 font-mono text-xs text-text-secondary">{p.ts_code}</td>
                            <td className="py-2 pr-3 text-text-primary">{p.name}</td>
                            <td className="py-2 pr-3 text-right tabular-nums text-text-primary">
                              {p.score.toFixed(1)}
                            </td>
                            <td className="py-2 pr-3 text-text-secondary">
                              {p.group}
                              {p.group_kind === 'industry' && (
                                <span className="ml-1 text-xs text-text-muted">(行业)</span>
                              )}
                            </td>
                            <td className="py-2 pr-3 text-right tabular-nums text-text-secondary">
                              {p.group_strength.toFixed(1)}
                            </td>
                            <td className="py-2 text-xs text-text-muted">{p.base_strategy}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-border/40 pt-4">
                    <Button onClick={() => review.mutate()} disabled={review.isPending || !!scan.review_task_id}>
                      <IconZap size={15} className="mr-1.5" />
                      {review.isPending ? '正在派发…' : '交给 Kimi 复核'}
                    </Button>
                    {scan.review_task_id ? (
                      <button
                        onClick={() => navigate(`/research/${scan.review_task_id}`)}
                        className="flex items-center gap-1 text-sm text-accent-blue hover:underline"
                      >
                        <IconCheck size={14} /> 查看复核报告与执行 trace
                      </button>
                    ) : (
                      <span className="text-xs text-text-muted">
                        Kimi Swarm 会去查龙虎榜席位、题材归属真伪、公告与减持——这些本地都没有数据源
                        （Tushare 的龙虎榜/涨停板接口在本账号下无访问权限），只能联网核实。
                        全过程 trace 会完整落盘。
                      </span>
                    )}
                  </div>
                </>
              )}

              {!!scan.rejected?.length && (
                <details className="mt-4">
                  <summary className="cursor-pointer text-xs text-text-muted hover:text-text-secondary">
                    落选 {scan.rejected.length} 只（买点成立但未通过板块筛选）
                  </summary>
                  <div className="mt-2 space-y-1">
                    {scan.rejected.map((r) => (
                      <div key={r.ts_code} className="flex flex-wrap gap-2 text-xs text-text-muted">
                        <span className="font-mono">{r.ts_code}</span>
                        <span className="text-text-secondary">{r.name}</span>
                        <span className="tabular-nums">确认分 {r.score.toFixed(1)}</span>
                        <span>{r.reason}</span>
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </>
          )}
        </>
      )}
    </Card>
  );
}

export default function DailyPick() {
  return (
    <div className="space-y-5 animate-fade-up">
      <PageHeader
        title="每日选股"
        description="活跃市值定开关 → 全市场 B1 买点确认 → 主线强弱选标的 → Kimi 复核资金面与消息面"
      />
      <AmvPanel />
      <ThemePanel />
      <ScanPanel />
    </div>
  );
}
