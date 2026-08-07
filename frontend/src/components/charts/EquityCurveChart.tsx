import ReactECharts from 'echarts-for-react';
import { BASE_TOOLTIP, CHART_COLORS } from '../../lib/chartTheme';

interface Props {
  equityCurve: [string, number][];
  height?: number;
}

export default function EquityCurveChart({ equityCurve, height = 300 }: Props) {
  if (!equityCurve || equityCurve.length === 0) {
    return <div className="flex h-40 items-center justify-center text-text-muted">暂无数据</div>;
  }

  const dates = equityCurve.map((e) => e[0]);
  const values = equityCurve.map((e) => e[1]);

  const option = {
    backgroundColor: 'transparent',
    tooltip: {
      ...BASE_TOOLTIP,
      trigger: 'axis',
      axisPointer: { type: 'line', lineStyle: { color: 'rgba(148,163,184,0.3)', type: 'dashed' } },
    },
    grid: { left: 60, right: 20, top: 20, bottom: 30 },
    xAxis: {
      type: 'category',
      data: dates,
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      axisLabel: { color: CHART_COLORS.axisLabel, fontSize: 10 },
    },
    yAxis: {
      type: 'value',
      scale: true,
      splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      axisLabel: { color: CHART_COLORS.axisLabel, fontSize: 10 },
      axisLine: { show: false },
    },
    series: [
      {
        type: 'line',
        data: values,
        smooth: true,
        lineStyle: { color: CHART_COLORS.gold, width: 2 },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(245, 185, 66, 0.28)' },
              { offset: 1, color: 'rgba(245, 185, 66, 0.02)' },
            ],
          },
        },
        symbol: 'none',
      },
    ],
  };

  return <ReactECharts option={option} style={{ height }} notMerge />;
}
