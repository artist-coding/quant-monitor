import api from './client';

export interface LibraryRoot {
  key: string;
  label: string;
  path: string;
  exists: boolean;
  count: number;
}

export interface LibraryItem {
  id: string;
  root: string;
  root_label: string;
  rel_path: string;
  name: string;
  title: string;
  description: string;
  category: string;
  size: number;
  modified_at: string;
  url: string;
}

export interface LibraryListResponse {
  roots: LibraryRoot[];
  items: LibraryItem[];
  total: number;
}

export async function fetchLibrary(): Promise<LibraryListResponse> {
  const { data } = await api.get<LibraryListResponse>('/library/');
  return data;
}
