import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

export interface MatrixVersion {
  id: string; matrix_key: string; version_no: number; name: string; status: string;
  channel_names: string[]; component_names: string[]; coefficients: number[][];
  coefficient_digest: string; condition_number?: number | null;
}
export interface ImageInfo { id: string; name: string; width: number; height: number; channels: string[]; }
export interface JobSnapshot {
  job_id: string; status: string; generation_no: number; required_tile_count: number;
  counts: Record<string, number>; quality_flags: Record<string, number>;
  error_code?: string | null; error_detail?: string | null; frozen_digest?: string | null;
}
export interface TileInfo {
  id: string; generation_no: number; level: number; x: number; y: number; kind: string;
  status: string; attempts: number; error_code?: string | null; cache_key?: string | null;
  quality_flags: Record<string, unknown>;
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private base = '/api';
  private token(): string { return localStorage.getItem('unmix.token') || ''; }
  constructor(private http: HttpClient) {}
  login(email: string, password: string): Observable<{access_token: string; role: string}> {
    return this.http.post<{access_token: string; role: string}>(`${this.base}/auth/login`, { email, password });
  }
  me(): Observable<any> { return this.http.get(`${this.base}/me`); }
  bootstrap(): Observable<any> { return this.http.post(`${this.base}/demo/bootstrap`, {}); }
  images(): Observable<ImageInfo[]> { return this.http.get<ImageInfo[]>(`${this.base}/images`); }
  matrices(): Observable<MatrixVersion[]> { return this.http.get<MatrixVersion[]>(`${this.base}/matrices`); }
  createMatrix(body: any): Observable<MatrixVersion> { return this.http.post<MatrixVersion>(`${this.base}/matrices`, body); }
  createJob(body: {image_id: string; matrix_version_id: string; algorithm: string}): Observable<any> {
    return this.http.post(`${this.base}/jobs`, body);
  }
  job(id: string): Observable<JobSnapshot> { return this.http.get<JobSnapshot>(`${this.base}/jobs/${id}`); }
  tiles(id: string): Observable<TileInfo[]> { return this.http.get<TileInfo[]>(`${this.base}/jobs/${id}/tiles`); }
  cancel(id: string): Observable<any> { return this.http.post(`${this.base}/jobs/${id}/cancel`, {}); }
  retry(id: string): Observable<any> { return this.http.post(`${this.base}/jobs/${id}/retry`, {}); }
  publish(id: string): Observable<any> { return this.http.post(`${this.base}/jobs/${id}/publish`, {}, { params: {} }); }
  sourceDzi(id: string): Observable<any> { return this.http.get(`${this.base}/images/${id}/dzi/source.json`); }
  jobDzi(id: string, kind: string): Observable<any> { return this.http.get(`${this.base}/jobs/${id}/dzi/${kind}.json`); }
  tileUrl(imageId: string, level: number, x: number, y: number, channel: number) {
    return `${this.base}/images/${imageId}/tiles/source/files/${level}/${x}_${y}.png?channel=${channel}&access_token=${encodeURIComponent(this.token())}`;
  }
  jobTileUrl(jobId: string, kind: string, level: number, x: number, y: number, channel: number) {
    return `${this.base}/jobs/${jobId}/tiles/${kind}/files/${level}/${x}_${y}.png?channel=${channel}&access_token=${encodeURIComponent(this.token())}`;
  }
}
