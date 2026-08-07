/** ECharts 通用暗色主题片段（与 globals.css 设计令牌保持一致） */
export const CHART_COLORS = {
  up: '#ef4444',
  down: '#22c55e',
  gold: '#f5b942',
  blue: '#4f8ef7',
  purple: '#9d6bf5',
  cyan: '#22c3d6',
  grid: 'rgba(148, 163, 184, 0.08)',
  axisLine: 'rgba(148, 163, 184, 0.16)',
  axisLabel: '#5d6c84',
  legend: '#8494ab',
};

/** 玻璃拟态 tooltip */
export const BASE_TOOLTIP = {
  backgroundColor: 'rgba(13, 18, 32, 0.95)',
  borderColor: 'rgba(148, 163, 184, 0.2)',
  borderWidth: 1,
  padding: [10, 14],
  textStyle: { color: '#e2e8f0', fontSize: 11 },
  extraCssText: 'border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,0.55);',
};

export const AXIS_POINTER_LINE = {
  lineStyle: { color: 'rgba(148, 163, 184, 0.35)', type: 'dashed' as const },
};
