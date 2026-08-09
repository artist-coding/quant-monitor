import api from './client';
import type { ResearchTask } from './research';

// ==================== 活跃市值 ====================

export interface AmvDay {
  trade_date: string;
  close: number;
  pct_chg: number | null;
  regime: string;
}

export interface AmvSegment {
  regime: string;
  start: string;
  end: string;
  days: number;
}

export interface AmvStatus {
  available: boolean;
  trade_date: string;
  close: number;
  pct_chg: number | null;
  regime: string;
  can_select: boolean;
  bull_threshold: number;
  bear_threshold: number;
  segments: AmvSegment[];
  recent: AmvDay[];
  precision_warning: string;
}

export async function fetchAmv(tradeDate?: string): Promise<AmvStatus> {
  const { data } = await api.get<AmvStatus>('/daily/amv', {
    params: tradeDate ? { trade_date: tradeDate } : undefined,
  });
  return data;
}

export async function addAmv(payload: {
  trade_date: string;
  close?: number;
  pct_chg?: number;
}): Promise<AmvStatus> {
  const { data } = await api.post<AmvStatus>('/daily/amv', payload);
  return data;
}

// ==================== 主线 ====================

export interface Theme {
  name: string;
  description: string;
  active: number;
  member_count: number;
  updated_at: string;
}

export interface ThemeStrength {
  theme: string;
  kind: string;
  strength: number;
  excess: number;
  rank: number;
  member_count: number;
  median_pct_chg: number;
  limit_up_count: number;
}

export interface ThemeRanking {
  trade_date: string;
  lookback: number;
  window: string[];
  themes: ThemeStrength[];
  industries: ThemeStrength[];
  dropped_themes: string[];
  reason: string;
}

export async function fetchThemes(): Promise<Theme[]> {
  const { data } = await api.get<{ themes: Theme[] }>('/daily/themes');
  return data.themes;
}

export async function upsertTheme(payload: {
  name: string;
  description?: string;
  active?: boolean;
}): Promise<Theme[]> {
  const { data } = await api.post<{ themes: Theme[] }>('/daily/themes', payload);
  return data.themes;
}

export async function removeTheme(name: string): Promise<Theme[]> {
  const { data } = await api.delete<{ themes: Theme[] }>(`/daily/themes/${encodeURIComponent(name)}`);
  return data.themes;
}

export async function setThemeMembers(
  name: string,
  codes: string[]
): Promise<{ theme: string; members: string[] }> {
  const { data } = await api.put(`/daily/themes/${encodeURIComponent(name)}/members`, { codes });
  return data;
}

export async function fetchThemeRanking(lookback = 5): Promise<ThemeRanking> {
  const { data } = await api.get<ThemeRanking>('/daily/themes/ranking', { params: { lookback } });
  return data;
}

// ==================== 扫描 ====================

export type ScanStatus = 'queued' | 'running' | 'completed' | 'failed';

export interface Pick {
  rank: number;
  ts_code: string;
  name: string;
  score: number;
  base_strategy: string;
  group: string;
  group_kind: string;
  group_strength: number;
  triggers: Array<Record<string, unknown>>;
  confirms: string[];
}

export interface RejectedPick {
  ts_code: string;
  name: string;
  score: number;
  reason: string;
}

export interface ScanTask {
  scan_id: string;
  status: ScanStatus;
  progress: number;
  message: string;
  created_at: string;
  started_at: string;
  completed_at: string;
  trade_date: string;
  amv: { trade_date?: string; close?: number; pct_chg?: number; regime?: string } | null;
  position_hint: { level?: string; range?: string; strength?: number; note?: string };
  market: Record<string, unknown>;
  warnings: string[];
  blocked: string;
  scanned: number;
  elapsed: number;
  counts: Record<string, number>;
  stopped: Record<string, number>;
  picks: Pick[];
  rejected: RejectedPick[];
  theme_ranking: { trade_date?: string; themes?: ThemeStrength[]; dropped_themes?: string[] };
  error: string;
  review_task_id: string;
}

export interface ScanParams {
  trade_date?: string;
  market_gate?: 'on' | 'off';
  top_n?: number;
  min_group_strength?: number;
  max_per_group?: number | null;
  include_watch?: boolean;
  theme_lookback?: number;
  save?: boolean;
}

export async function createScan(params: ScanParams = {}): Promise<ScanTask> {
  const { data } = await api.post<ScanTask>('/daily/scan', params);
  return data;
}

export async function fetchScan(scanId: string): Promise<ScanTask> {
  const { data } = await api.get<ScanTask>(`/daily/scan/${scanId}`);
  return data;
}

export async function fetchLatestScan(): Promise<ScanTask | null> {
  try {
    const { data } = await api.get<ScanTask>('/daily/scan/latest');
    return data;
  } catch {
    // 还没跑过任何扫描时后端返回 404，这是正常初始状态，不当错误处理
    return null;
  }
}

export async function createReview(scanId: string): Promise<ResearchTask> {
  const { data } = await api.post<ResearchTask>(`/daily/scan/${scanId}/review`);
  return data;
}
