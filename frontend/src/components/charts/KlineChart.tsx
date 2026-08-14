import { useEffect, useRef } from 'react';
import ReactECharts from 'echarts-for-react';
import type { KlineChart as KlineDataType } from '../../api/types';
import type { KlinePeriod } from '../../api/stock';
import { SIGNAL_COLORS } from '../../lib/constants';
import { BASE_TOOLTIP, CHART_COLORS } from '../../lib/chartTheme';
import { formatVolumeAxis, formatNumber } from '../../lib/formatters';

interface Props {
  data: KlineDataType;
  height?: number;
  period?: KlinePeriod;
  onPeriodChange?: (p: KlinePeriod) => void;
}

export default function KlineChart({ data, height = 820, period = 'daily', onPeriodChange }: Props) {
  const {
    dates,
    ohlc,
    volumes,
    pct_chgs,
    overlays,
    signal_markers,
    kdj,
    macd,
  } = data;

  const upColor = '#ef4444';
  const downColor = '#22c55e';

  // 触控板两指上下滑要滚动页面，只有捏合缩放才动 K 线。
  //
  // 光靠 dataZoom 的 zoomOnMouseWheel:'ctrl' 不够：ECharts 的 wheel 监听挂在
  // 内部 DOM 上且非 passive，事件一旦进到 zrender 就有被 preventDefault 的机会，
  // 表现就是鼠标停在图上时页面卡住不动。这里在外层容器的**捕获阶段**先拦一道，
  // 非 Ctrl 的滚轮直接 stopPropagation —— ECharts 根本收不到，浏览器默认滚动保留。
  // 浏览器把触控板捏合手势就是当作 Ctrl+wheel 下发的，所以缩放不受影响。
  const wheelGuardRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = wheelGuardRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) e.stopPropagation();
    };
    el.addEventListener('wheel', onWheel, { capture: true, passive: true });
    return () => el.removeEventListener('wheel', onWheel, { capture: true });
  }, []);

  // 取每个 series 的最后一个非 null 值，用作右侧点位标签
  const lastValid = (arr: (number | null)[]): number | null => {
    for (let i = arr.length - 1; i >= 0; i--) {
      if (arr[i] !== null && arr[i] !== undefined) return arr[i];
    }
    return null;
  };

  const lastWhite = lastValid(overlays.white_line);
  const lastYellow = lastValid(overlays.yellow_line);
  const lastBbi = lastValid(overlays.bbi);
  const lastDate = dates[dates.length - 1];

  // 构建 markPoint 数据
  const buyMarkers = signal_markers
    .filter((m) => m.action === 'BUY')
    .map((m) => ({
      name: m.type,
      coord: [m.date, m.price],
      value: m.type,
      itemStyle: { color: SIGNAL_COLORS[m.type] || SIGNAL_COLORS.BUY },
    }));

  const sellMarkers = signal_markers
    .filter((m) => m.action === 'SELL')
    .map((m) => ({
      name: m.type,
      coord: [m.date, m.price],
      value: m.type,
      itemStyle: { color: SIGNAL_COLORS[m.type] || SIGNAL_COLORS.SELL },
    }));

  /** 自定义悬浮框：默认的 candlestick tooltip 只有开高低收，这里补上涨跌幅与成交量 */
  const tooltipFormatter = (params: Array<Record<string, any>>) => {
    if (!Array.isArray(params) || params.length === 0) return '';
    const idx = params[0].dataIndex as number;
    const date = params[0].axisValue as string;
    const bar = ohlc[idx];
    if (!bar) return '';
    const [open, close, low, high] = bar;
    const pct = pct_chgs[idx];
    const pctColor = pct >= 0 ? upColor : downColor;
    const pctText = `${pct >= 0 ? '+' : ''}${(pct ?? 0).toFixed(2)}%`;
    const row = (label: string, value: string, color = '#e2e8f0') =>
      `<div style="display:flex;justify-content:space-between;gap:16px;line-height:1.7">` +
      `<span style="color:#8494ab">${label}</span><span style="color:${color};font-variant-numeric:tabular-nums">${value}</span></div>`;

    let html = `<div style="font-weight:bold;margin-bottom:4px">${date}</div>`;
    html += row('涨跌幅', pctText, pctColor);
    html += row('开盘', formatNumber(open));
    html += row('最高', formatNumber(high), upColor);
    html += row('最低', formatNumber(low), downColor);
    html += row('收盘', formatNumber(close), close >= open ? upColor : downColor);
    html += row('成交量', formatVolumeAxis(volumes[idx] ?? 0));

    // 其余折线（白线/黄线/BBI/KDJ/MACD…）按所在序列附在下面
    const skip = new Set(['K线', '成交量']);
    const lines = params.filter(
      (p) => !skip.has(p.seriesName) && p.value !== null && p.value !== undefined && typeof p.value === 'number'
    );
    if (lines.length > 0) {
      html += `<div style="border-top:1px solid rgba(148,163,184,0.2);margin:6px 0 4px"></div>`;
      for (const p of lines) {
        html += row(`${p.marker ?? ''}${p.seriesName}`, formatNumber(p.value as number));
      }
    }
    return html;
  };

  /** 每个子图左上角的指标名标签。样式集中在这里，免得 5 份重复配置 */
  const paneLabel = (text: string, top: string, rich?: Record<string, Record<string, unknown>>) => ({
    text,
    left: 66,
    top,
    textStyle: {
      color: '#8494ab',
      fontSize: 11,
      fontWeight: 'bold' as const,
      rich,
    },
    backgroundColor: 'rgba(13, 18, 32, 0.55)',
    padding: [2, 6, 2, 6],
    borderRadius: 4,
  });

  const option = {
    backgroundColor: 'transparent',
    animation: false,
    // 子图指标名标注（top 与对应 grid 的 top 保持一致）
    title: [
      paneLabel('成交量', '50%'),
      paneLabel('KDJ(9,3,3)  {k|K} {d|D} {j|J}', '66%', {
        k: { color: '#f59e0b', fontSize: 11, fontWeight: 'bold' },
        d: { color: '#3b82f6', fontSize: 11, fontWeight: 'bold' },
        j: { color: '#a855f7', fontSize: 11, fontWeight: 'bold' },
      }),
      paneLabel('MACD(12,26,9)  {dif|DIF} {dea|DEA} {hist|柱}', '82%', {
        dif: { color: '#f59e0b', fontSize: 11, fontWeight: 'bold' },
        dea: { color: '#3b82f6', fontSize: 11, fontWeight: 'bold' },
        hist: { color: '#8494ab', fontSize: 11 },
      }),
    ],
    tooltip: {
      ...BASE_TOOLTIP,
      trigger: 'axis',
      formatter: tooltipFormatter,
      axisPointer: { type: 'cross', lineStyle: { color: 'rgba(148, 163, 184, 0.35)' }, label: { backgroundColor: 'rgba(23, 30, 51, 0.95)', borderColor: 'rgba(148,163,184,0.2)', borderWidth: 1, color: '#e2e8f0' } },
    },
    legend: {
      data: ['K线', '白线', '黄线', 'BBI'],
      top: 0,
      textStyle: { color: CHART_COLORS.legend, fontSize: 11 },
      itemWidth: 14,
      itemHeight: 2,
    },
    // 四个 grid 的 left/right 必须完全一致：绘图区宽度不同会导致同一天的
    // K 线与下方成交量/KDJ/MACD 柱在横向上错位。right 统一取 70，
    // 给主图右侧的白线/黄线/BBI 点位标签留出空间。
    grid: [
      { left: 60, right: 70, top: 30, height: '43%' },
      { left: 60, right: 70, top: '50%', height: '12%' },
      { left: 60, right: 70, top: '66%', height: '13%' },
      { left: 60, right: 70, top: '82%', height: '13%' },
    ],
    xAxis: [
      {
        type: 'category',
        data: dates,
        gridIndex: 0,
        axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
        axisLabel: { color: CHART_COLORS.axisLabel, fontSize: 10 },
        splitLine: { show: false },
      },
      {
        type: 'category',
        data: dates,
        gridIndex: 1,
        axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
        axisLabel: { show: false },
        splitLine: { show: false },
      },
      {
        type: 'category',
        data: dates,
        gridIndex: 2,
        axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
        axisLabel: { show: false },
        splitLine: { show: false },
      },
      {
        type: 'category',
        data: dates,
        gridIndex: 3,
        axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
        axisLabel: { show: false },
        splitLine: { show: false },
      },
    ],
    yAxis: [
      {
        scale: true,
        gridIndex: 0,
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
        axisLabel: { color: CHART_COLORS.axisLabel, fontSize: 10 },
        axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      },
      {
        scale: true,
        gridIndex: 1,
        splitLine: { show: false },
        axisLabel: {
          color: CHART_COLORS.axisLabel,
          fontSize: 10,
          formatter: (v: number) => formatVolumeAxis(v),
        },
        axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      },
      {
        scale: true,
        gridIndex: 2,
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
        axisLabel: { color: CHART_COLORS.axisLabel, fontSize: 10 },
        axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      },
      {
        scale: true,
        gridIndex: 3,
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
        axisLabel: { color: CHART_COLORS.axisLabel, fontSize: 10 },
        axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      },
    ],
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: [0, 1, 2, 3],
        start: 60,
        end: 100,
        // 'ctrl'：只有按住 Ctrl 的滚轮才缩放。触控板的捏合手势在浏览器里
        // 就是以 Ctrl+wheel 事件下发的，所以效果是：捏合 → 缩放K线；
        // 双指上下滑 → 事件不被图表拦截，正常滚动页面。
        zoomOnMouseWheel: 'ctrl',
        moveOnMouseWheel: false,
      },
      { type: 'slider', xAxisIndex: [0, 1, 2, 3], bottom: 5, height: 15, borderColor: 'rgba(148, 163, 184, 0.2)', backgroundColor: 'rgba(9, 12, 22, 0.5)', fillerColor: 'rgba(245, 185, 66, 0.12)', handleStyle: { color: CHART_COLORS.gold }, moveHandleStyle: { color: CHART_COLORS.gold }, textStyle: { color: CHART_COLORS.axisLabel } },
    ],
    series: [
      {
        name: 'K线',
        type: 'candlestick',
        data: ohlc,
        xAxisIndex: 0,
        yAxisIndex: 0,
        itemStyle: {
          color: upColor,
          color0: downColor,
          borderColor: upColor,
          borderColor0: downColor,
        },
        markPoint: {
          symbol: 'triangle',
          symbolSize: 10,
          data: [
            ...buyMarkers.map((m) => ({
              ...m,
              symbol: 'triangle',
              symbolRotate: 0,
              symbolOffset: [0, 10],
            })),
            ...sellMarkers.map((m) => ({
              ...m,
              symbol: 'triangle',
              symbolRotate: 180,
              symbolOffset: [0, -10],
            })),
          ],
          label: { show: false },
        },
      },
      // 白线 (EMA(EMA(C,10),10)) - 短期动能线
      {
        name: '白线',
        type: 'line',
        data: overlays.white_line,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        lineStyle: { width: 2, color: '#ffffff' },
        symbol: 'none',
        // 右侧点位标签
        markPoint: lastWhite !== null ? {
          symbol: 'roundRect',
          symbolSize: [44, 18],
          symbolOffset: [28, 0],
          data: [{
            coord: [lastDate, lastWhite],
            value: formatNumber(lastWhite),
            itemStyle: { color: '#0b0f19', borderColor: '#ffffff', borderWidth: 1 },
            label: { color: '#ffffff', fontSize: 10, fontWeight: 'bold' },
          }],
        } : undefined,
      },
      // 黄线 ((MA14+MA28+MA57+MA114)/4) - 多空生命线
      {
        name: '黄线',
        type: 'line',
        data: overlays.yellow_line,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        lineStyle: { width: 2, color: '#fbbf24' },
        symbol: 'none',
        markPoint: lastYellow !== null ? {
          symbol: 'roundRect',
          symbolSize: [44, 18],
          symbolOffset: [28, 0],
          data: [{
            coord: [lastDate, lastYellow],
            value: formatNumber(lastYellow),
            itemStyle: { color: '#0b0f19', borderColor: '#fbbf24', borderWidth: 1 },
            label: { color: '#fbbf24', fontSize: 10, fontWeight: 'bold' },
          }],
        } : undefined,
      },
      // BBI 多空指数
      {
        name: 'BBI',
        type: 'line',
        data: overlays.bbi,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        lineStyle: { width: 1.5, color: '#06b6d4', type: 'dashed' },
        symbol: 'none',
        markPoint: lastBbi !== null ? {
          symbol: 'roundRect',
          symbolSize: [44, 18],
          symbolOffset: [28, 0],
          data: [{
            coord: [lastDate, lastBbi],
            value: formatNumber(lastBbi),
            itemStyle: { color: '#0b0f19', borderColor: '#06b6d4', borderWidth: 1 },
            label: { color: '#06b6d4', fontSize: 10, fontWeight: 'bold' },
          }],
        } : undefined,
      },
      // 成交量
      {
        name: '成交量',
        type: 'bar',
        data: volumes.map((v, i) => ({
          value: v,
          itemStyle: { color: pct_chgs[i] >= 0 ? `${upColor}80` : `${downColor}80` },
        })),
        xAxisIndex: 1,
        yAxisIndex: 1,
      },
      // KDJ
      {
        name: 'K',
        type: 'line',
        data: kdj.k,
        xAxisIndex: 2,
        yAxisIndex: 2,
        smooth: true,
        lineStyle: { width: 1, color: '#f59e0b' },
        symbol: 'none',
      },
      {
        name: 'D',
        type: 'line',
        data: kdj.d,
        xAxisIndex: 2,
        yAxisIndex: 2,
        smooth: true,
        lineStyle: { width: 1, color: '#3b82f6' },
        symbol: 'none',
      },
      {
        name: 'J',
        type: 'line',
        data: kdj.j,
        xAxisIndex: 2,
        yAxisIndex: 2,
        smooth: true,
        lineStyle: { width: 1, color: '#a855f7' },
        symbol: 'none',
      },
      // MACD
      {
        name: 'DIF',
        type: 'line',
        data: macd.dif,
        xAxisIndex: 3,
        yAxisIndex: 3,
        smooth: true,
        lineStyle: { width: 1, color: '#f59e0b' },
        symbol: 'none',
      },
      {
        name: 'DEA',
        type: 'line',
        data: macd.dea,
        xAxisIndex: 3,
        yAxisIndex: 3,
        smooth: true,
        lineStyle: { width: 1, color: '#3b82f6' },
        symbol: 'none',
      },
      {
        name: 'MACD',
        type: 'bar',
        data: macd.hist.map((v) => ({
          value: v,
          itemStyle: { color: (v ?? 0) >= 0 ? `${upColor}80` : `${downColor}80` },
        })),
        xAxisIndex: 3,
        yAxisIndex: 3,
      },
    ],
  };

  return (
    <div className="space-y-4" ref={wheelGuardRef}>
      {/* 周期切换 */}
      <div className="flex items-center justify-between pb-3 mb-1 border-b border-border/30">
        <div className="text-[11px] text-text-muted font-medium tracking-wider uppercase">K线周期</div>
        <div className="flex items-center gap-0.5 bg-bg-primary/70 p-0.5 rounded-lg border border-border/60">
          {(
            [
              { key: 'daily', label: '日线' },
              { key: 'weekly', label: '周线' },
            ] as const
          ).map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => onPeriodChange?.(t.key)}
              className={`px-3 py-1 text-[11px] font-medium rounded-md transition-all ${
                period === t.key
                  ? 'bg-accent-gold/15 text-accent-gold shadow-[inset_0_0_0_1px_rgba(245,185,66,0.35)]'
                  : 'text-text-muted hover:text-text-primary'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
      <ReactECharts option={option} style={{ height }} notMerge />
    </div>
  );
}
