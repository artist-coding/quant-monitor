import api from './client';
import type { ScreenResult, StrategyInfo } from './types';

export async function fetchStrategies(): Promise<StrategyInfo[]> {
  const { data } = await api.get<StrategyInfo[]>('/screen/strategies');
  return data;
}

// limit 只截输出条数，maxStocks 才是扫描范围（0=全市场）。
// 两者曾经是同一个参数，结果"筛选 20 条"等于"只扫 20 只股票"。
export async function runScreen(strategy: string, limit = 20, maxStocks = 0): Promise<ScreenResult> {
  const { data } = await api.post<ScreenResult>('/screen/run', {
    strategy,
    limit,
    max_stocks: maxStocks,
    use_parallel: true,
  });
  return data;
}
