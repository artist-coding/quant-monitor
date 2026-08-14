import { useQuery } from '@tanstack/react-query';
import { fetchStockAnalysis, fetchKlineData, fetchCommentary, type KlinePeriod } from '../api/stock';

export function useStockAnalysis(tsCode: string, days = 120) {
  return useQuery({
    queryKey: ['stock', tsCode, days],
    queryFn: () => fetchStockAnalysis(tsCode, days),
    enabled: !!tsCode,
    staleTime: 5 * 60 * 1000,
  });
}

export function useKlineData(tsCode: string, days = 120, period: KlinePeriod = 'daily') {
  return useQuery({
    queryKey: ['kline', tsCode, days, period],
    queryFn: () => fetchKlineData(tsCode, days, period),
    enabled: !!tsCode,
    staleTime: 5 * 60 * 1000,
    // 日线/周线切换时保留旧图，避免整页闪加载态
    placeholderData: (prev) => prev,
  });
}

export function useCommentary(tsCode: string, days = 120) {
  return useQuery({
    queryKey: ['commentary', tsCode, days],
    queryFn: () => fetchCommentary(tsCode, days),
    enabled: !!tsCode,
    staleTime: 60 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    retry: 1,
  });
}
