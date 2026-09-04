// 策略列表
export const STRATEGIES = [
  { alias: 'B1', label: 'B1 买点' },
  { alias: 'B2', label: 'B2 确认' },
  { alias: 'B3', label: 'B3 共识' },
  { alias: '完美图形', label: '完美图形' },
  { alias: '超级B1', label: '超级B1' },
  { alias: '长安战法', label: '长安战法' },
  { alias: '建仓波', label: '建仓波' },
  { alias: '吸筹', label: '吸筹' },
  { alias: '安全', label: '安全' },
  { alias: '超跌', label: '超跌' },
  { alias: '突破', label: '突破' },
] as const;

// 信号颜色映射
export const SIGNAL_COLORS: Record<string, string> = {
  B1: '#22c55e',
  B2: '#3b82f6',
  B3: '#a855f7',
  SB1: '#06b6d4',
  S1: '#ef4444',
  S2: '#f97316',
  S3: '#eab308',
  BUY: '#22c55e',
  SELL: '#ef4444',
  HOLD: '#f59e0b',
  WATCH: '#64748b',
};

// 风险等级颜色
export const RISK_COLORS: Record<string, string> = {
  LOW: '#22c55e',
  MEDIUM: '#f59e0b',
  HIGH: '#f97316',
  CRITICAL: '#ef4444',
  UNKNOWN: '#64748b',
};

// 导航菜单（icon 为 icons.tsx 中的图标键名）
export const NAV_ITEMS = [
  { path: '/', label: '总览', icon: 'grid' },
  { path: '/daily', label: '每日选股', icon: 'radar' },
  { path: '/research', label: '分析记录', icon: 'archive' },
  { path: '/library', label: '资料库', icon: 'book' },
  { path: '/screen', label: '选股', icon: 'target' },
  { path: '/watchlist', label: '自选', icon: 'star' },
  { path: '/backtest', label: '回测', icon: 'history' },
  { path: '/simulator', label: '模拟', icon: 'activity' },
  { path: '/trades', label: '交易', icon: 'exchange' },
  { path: '/settings', label: '设置', icon: 'settings' },
] as const;
