import ReactECharts from 'echarts-for-react';
import type { SimulationEquityPoint } from '../../api/types';
import { BASE_TOOLTIP, CHART_COLORS } from '../../lib/chartTheme';

interface Props {
  equityCurve: SimulationEquityPoint[];
  benchmarkCurve: Array<{ date: string; close: number }>;
  height?: number;
}

export default function SimulatorEquityCurveChart({
  equityCurve,
  benchmarkCurve,
  height = 360,
}: Props) {
  if (!equityCurve || equityCurve.length === 0) {
    return (
      <div className="flex h-40 items-center justify-center text-text-muted">暂无数据</div>
    );
  }

  const dates = equityCurve.map((e) => e.date);
  const equities = equityCurve.map((e) => e.equity);

  // 计算回撤序列
  const drawdowns = equities.reduce(
    (acc, val) => {
      if (val > acc.peak) acc.peak = val;
      const dd = acc.peak > 0 ? -((acc.peak - val) / acc.peak) * 100 : 0;
      acc.values.push(dd);
      return acc;
    },
    { peak: equities[0], values: [] as number[] }
  ).values;

  // 对齐基准曲线到 equity 日期
  const benchMap = new Map(benchmarkCurve.map((b) => [b.date, b.close]));
  const firstBench = benchmarkCurve[0]?.close;
  const benchmarkValues = dates.map((date) => {
    const close = benchMap.get(date);
    if (close == null || !firstBench || firstBench === 0) return null;
    return (close / firstBench) * equities[0];
  });

  // 市场环境背景色
  const markAreas: Array<[object, object, object]> = [];
  let currentRegime = equityCurve[0]?.regime;
  let regimeStart = 0;
  equityCurve.forEach((point, idx) => {
    if (point.regime !== currentRegime || idx === equityCurve.length - 1) {
      const endIdx = idx === equityCurve.length - 1 ? idx : idx - 1;
      const color = currentRegime === '强势'
        ? 'rgba(34, 197, 94, 0.04)'
        : currentRegime === '弱势'
          ? 'rgba(239, 68, 68, 0.04)'
          : 'rgba(245, 185, 66, 0.03)';
      markAreas.push([
        {
          xAxis: dates[regimeStart],
          itemStyle: { color },
        },
        {
          xAxis: dates[endIdx],
        },
        {
          name: currentRegime,
        },
      ]);
      currentRegime = point.regime;
      regimeStart = idx;
    }
  });

  const option = {
    backgroundColor: 'transparent',
    tooltip: {
      ...BASE_TOOLTIP,
      trigger: 'axis',
      axisPointer: { type: 'line', lineStyle: { color: 'rgba(148,163,184,0.3)', type: 'dashed' } },
      formatter: (params: Array<{ seriesName: string; value: number | null; axisValue: string }>) => {
        const lines = [`<div class="font-bold">${params[0]?.axisValue}</div>`];
        params.forEach((p) => {
          if (p.value == null) return;
          const label = p.seriesName;
          const val = typeof p.value === 'number' ? p.value.toFixed(2) : p.value;
          lines.push(`${label}: ${val}`);
        });
        return lines.join('<br/>');
      },
    },
    legend: {
      data: ['权益', '基准', '回撤'],
      textStyle: { color: CHART_COLORS.legend, fontSize: 11 },
      top: 0,
    },
    grid: [
      { left: 60, right: 20, top: 35, bottom: 110 },
      { left: 60, right: 20, top: 260, bottom: 50 },
    ],
    xAxis: [
      {
        type: 'category',
        data: dates,
        gridIndex: 0,
        axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
        axisLabel: { show: false },
      },
      {
        type: 'category',
        data: dates,
        gridIndex: 1,
        axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
        axisLabel: { color: CHART_COLORS.axisLabel, fontSize: 10 },
      },
    ],
    yAxis: [
      {
        type: 'value',
        gridIndex: 0,
        scale: true,
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
        axisLabel: { color: CHART_COLORS.axisLabel, fontSize: 10 },
        axisLine: { show: false },
      },
      {
        type: 'value',
        gridIndex: 1,
        scale: true,
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
        axisLabel: {
          color: CHART_COLORS.axisLabel,
          fontSize: 10,
          formatter: '{value}%',
        },
        axisLine: { show: false },
      },
    ],
    dataZoom: [
      {
        type: 'slider',
        xAxisIndex: [0, 1],
        bottom: 10,
        height: 22,
        borderColor: 'rgba(148, 163, 184, 0.2)',
        backgroundColor: 'rgba(9, 12, 22, 0.5)',
        fillerColor: 'rgba(245, 185, 66, 0.15)',
        handleStyle: { color: CHART_COLORS.gold },
        moveHandleStyle: { color: CHART_COLORS.gold },
        textStyle: { color: CHART_COLORS.axisLabel },
      },
    ],
    series: [
      {
        name: '权益',
        type: 'line',
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: equities,
        smooth: true,
        lineStyle: { color: CHART_COLORS.blue, width: 2 },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(79, 142, 247, 0.18)' },
              { offset: 1, color: 'rgba(79, 142, 247, 0.01)' },
            ],
          },
        },
        symbol: 'none',
        markArea: {
          data: markAreas,
          silent: true,
        },
      },
      {
        name: '基准',
        type: 'line',
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: benchmarkValues,
        smooth: true,
        lineStyle: { color: CHART_COLORS.gold, width: 1.5, type: 'dashed' },
        symbol: 'none',
      },
      {
        name: '回撤',
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: drawdowns,
        smooth: true,
        lineStyle: { color: CHART_COLORS.up, width: 1 },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(239, 68, 68, 0.4)' },
              { offset: 1, color: 'rgba(239, 68, 68, 0.02)' },
            ],
          },
        },
        symbol: 'none',
      },
    ],
  };

  return <ReactECharts option={option} style={{ height }} notMerge />;
}
