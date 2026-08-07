import ReactECharts from 'echarts-for-react';
import type { ScoreDetail } from '../../api/types';
import { CHART_COLORS } from '../../lib/chartTheme';

interface Props {
  score: ScoreDetail;
  height?: number;
}

export default function RadarChart({ score, height = 220 }: Props) {
  const option = {
    backgroundColor: 'transparent',
    radar: {
      indicator: [
        { name: 'B1', max: 100 },
        { name: '趋势', max: 100 },
        { name: '量价', max: 100 },
        { name: '风险', max: 100 },
      ],
      shape: 'polygon',
      splitNumber: 4,
      axisName: { color: CHART_COLORS.legend, fontSize: 12, fontWeight: 500 },
      splitLine: { lineStyle: { color: CHART_COLORS.grid, width: 1 } },
      splitArea: {
        areaStyle: {
          color: ['rgba(245, 185, 66, 0.02)', 'rgba(245, 185, 66, 0.04)', 'rgba(245, 185, 66, 0.06)', 'rgba(245, 185, 66, 0.08)'],
        },
      },
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine, width: 1 } },
    },
    series: [
      {
        type: 'radar',
        data: [
          {
            value: [score.b1_score, score.trend_score, score.volume_score, score.risk_score],
            name: '评分',
            areaStyle: {
              color: {
                type: 'radial',
                x: 0.5, y: 0.5, r: 0.5,
                colorStops: [
                  { offset: 0, color: 'rgba(245, 185, 66, 0.32)' },
                  { offset: 1, color: 'rgba(245, 185, 66, 0.05)' },
                ],
              },
            },
            lineStyle: { color: CHART_COLORS.gold, width: 2 },
            itemStyle: { color: CHART_COLORS.gold, borderColor: '#fff', borderWidth: 1 },
          },
        ],
      },
    ],
  };

  return <ReactECharts option={option} style={{ height }} notMerge />;
}
