import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Injectable, effect, signal } from '@angular/core';
import { Observable } from 'rxjs';
import {
  ImageInfo,
  JobInfo,
  MatrixInfo,
  Me,
  ResultInfo,
} from './models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  /**
   * The API key is stored per browser session only. All tile requests carry
   * it, which is important because tile URLs embed the pinned generation and
   * are cached as immutable — the key lives in a header, never in the URL.
   */
  readonly apiKey = signal<string>(localStorage.getItem('unmix-api-key') ?? '');
  /**
   * Same-origin by default (nginx proxies /viewer, /jobs, … to the API in
   * the container deployment). For `ng serve` dev, set http://localhost:8000.
   */
  readonly baseUrl = '';

  constructor(private http: HttpClient) {
    effect(() => localStorage.setItem('unmix-api-key', this.apiKey()));
  }

  private headers(): HttpHeaders {
    return new HttpHeaders({ 'X-API-Key': this.apiKey() });
  }

  me(): Observable<Me> {
    return this.http.get<Me>(`${this.baseUrl}/me`, { headers: this.headers() });
  }

  images(): Observable<ImageInfo[]> {
    return this.http.get<ImageInfo[]>(`${this.baseUrl}/images`, {
      headers: this.headers(),
    });
  }

  createSynthetic(name: string, h: number, w: number) {
    return this.http.post<{ id: number; digest: string }>(
      `${this.baseUrl}/images/synthetic`,
      { name, height: h, width: w, channels: 4, components: 3, seed: 7 },
      { headers: this.headers() },
    );
  }

  matrices(): Observable<MatrixInfo[]> {
    return this.http.get<MatrixInfo[]>(`${this.baseUrl}/matrices`, {
      headers: this.headers(),
    });
  }

  publishMatrix(name: string, matrix: number[][]): Observable<MatrixInfo> {
    return this.http.post<MatrixInfo>(
      `${this.baseUrl}/matrices`,
      { name, matrix },
      { headers: this.headers() },
    );
  }

  createJob(imageId: number, matrixVersionId?: number) {
    return this.http.post<{ id: number }>(
      `${this.baseUrl}/jobs`,
      { image_id: imageId, matrix_version_id: matrixVersionId ?? null },
      { headers: this.headers() },
    );
  }

  job(id: number): Observable<JobInfo> {
    return this.http.get<JobInfo>(`${this.baseUrl}/jobs/${id}`, {
      headers: this.headers(),
    });
  }

  cancelJob(id: number) {
    return this.http.post(`${this.baseUrl}/jobs/${id}/cancel`, {}, {
      headers: this.headers(),
    });
  }

  retryJob(id: number) {
    return this.http.post<{ new_generation_id: number }>(
      `${this.baseUrl}/jobs/${id}/retry`,
      {},
      { headers: this.headers() },
    );
  }

  release(jobId: number) {
    return this.http.post<ResultInfo | { detail: unknown }>(
      `${this.baseUrl}/results/release`,
      { job_id: jobId },
      { headers: this.headers() },
    );
  }

  results(): Observable<ResultInfo[]> {
    return this.http.get<ResultInfo[]>(`${this.baseUrl}/results`, {
      headers: this.headers(),
    });
  }

  /**
   * DZI tile source URL. The path pins job + generation; the layer is a query
   * parameter so raw/component/residual share one immutable cache namespace.
   * Credentials are sent via OpenSeadragon's XHR header hook, never embedded
   * in the URL (which would also fragment the shared browser cache).
   */
  dziUrl(jobId: number, genId: number, view: string): string {
    return `${this.baseUrl}/viewer/jobs/${jobId}/generations/${genId}/${view}.dzi`;
  }
}
