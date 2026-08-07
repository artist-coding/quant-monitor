import api from './client';

export type ResearchStatus = 'queued' | 'running' | 'completed' | 'failed';

export interface ResearchTask {
  task_id: string;
  ts_code: string;
  status: ResearchStatus;
  progress: number;
  message: string;
  report: string;
  error: string;
  created_at: string;
  started_at: string;
  completed_at: string;
  engine: string;
  mode: string;
  skill: string;
  trace_id: string;
  trace_available: boolean;
  trace_schema_version: number;
  agent_count: number;
  expected_agent_count: number;
  partial_result: boolean;
  partial_reason: string;
  last_activity_at: string;
  target_type: 'stock' | 'theme';
}

export interface ResearchTaskSummary {
  task_id: string;
  ts_code: string;
  status: ResearchStatus;
  progress: number;
  message: string;
  error: string;
  created_at: string;
  started_at: string;
  completed_at: string;
  agent_count: number;
  expected_agent_count: number;
  partial_result: boolean;
  target_type: 'stock' | 'theme';
  has_report: boolean;
  report_excerpt: string;
}

export interface ResearchHistoryResponse {
  tasks: ResearchTaskSummary[];
  total: number;
  page: number;
  page_size: number;
  status_counts: Record<string, number>;
}

export async function createResearch(tsCode: string): Promise<ResearchTask> {
  const { data } = await api.post<ResearchTask>('/research/', { ts_code: tsCode });
  return data;
}

export async function fetchResearch(taskId: string): Promise<ResearchTask> {
  const { data } = await api.get<ResearchTask>(`/research/${taskId}`);
  return data;
}

export async function fetchResearchHistory(params: {
  page?: number;
  pageSize?: number;
  status?: ResearchStatus;
  keyword?: string;
}): Promise<ResearchHistoryResponse> {
  const { data } = await api.get<ResearchHistoryResponse>('/research/history', {
    params: {
      page: params.page || 1,
      page_size: params.pageSize || 20,
      status: params.status,
      keyword: params.keyword || undefined,
    },
  });
  return data;
}
