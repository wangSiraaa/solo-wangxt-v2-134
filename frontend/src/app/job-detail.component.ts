import { Component, OnDestroy, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { Subscription, interval } from 'rxjs';
import { ApiService } from './api.service';
import { JobInfo } from './models';
import { ViewerComponent } from './viewer/viewer.component';

type View = 'raw' | 'components' | 'residual';

@Component({
  selector: 'app-job-detail',
  standalone: true,
  imports: [CommonModule, FormsModule, ViewerComponent, RouterLink],
  template: `
    @if (job) {
      <div class="head">
        <h2>任务 #{{ job.id }} <span class="status" [class]="job.status">{{ statusLabel(job.status) }}</span></h2>
        <div class="pins">
          <span title="图像内容摘要（任务启动时固定）">图像 <code>{{ job.image_digest.slice(0, 16) }}…</code></span>
          <span title="矩阵内容摘要（任务启动时固定；换版不影响本任务）">矩阵 <code>{{ job.matrix_digest.slice(0, 16) }}…</code></span>
          <span>算法 <code>{{ job.algorithm }}</code></span>
          <span title="整图统计参数先冻结、再切片；各切片不可自行估计">参数哈希 <code>{{ (job.params_hash ?? '冻结中').slice(0, 16) }}</code></span>
        </div>
        @if (job.successor_job_id) {
          <div class="warn">矩阵已换版 → 本任务已被废弃，<a [routerLink]="['/jobs', job.successor_job_id]">查看新代次任务 #{{ job.successor_job_id }}</a></div>
        }
        @if (job.error_code) {
          <div class="warn">错误：{{ job.error_code }} — {{ job.error_detail }}</div>
        }
        @if (issues().length) {
          <div class="warn">
            @for (i of issues(); track $index) {
              <div>[{{ i.severity }}] {{ i.code }} — {{ i.message }}</div>
            }
          </div>
        }
        @if (maxSaturation() > 0) {
          <div class="warn">检测到饱和：最高通道 {{ (maxSaturation() * 100).toFixed(2) }}% 像素达到探测器上限（已计入冻结的整图统计）</div>
        }
        <div class="actions">
          <button (click)="cancel()" [disabled]="!active">取消</button>
          <button (click)="retry()" [disabled]="active">重试（新代次，沿用同一模型）</button>
          <button class="pub" (click)="publish()" [disabled]="!canRelease">正式发布</button>
        </div>
        @if (publishMsg) { <div [class.ok]="publishOk" class="pubmsg">{{ publishMsg }}</div> }
      </div>

      <div class="controls">
        <div class="seg">
          <button [class.on]="view === 'raw'" (click)="setView('raw')">原通道</button>
          <button [class.on]="view === 'components'" (click)="setView('components')">估计成分</button>
          <button [class.on]="view === 'residual'" (click)="setView('residual')">残差</button>
        </div>
        <label>代次
          <select [(ngModel)]="genId" (ngModelChange)="onGen()">
            @for (g of job.generations; track g.id) {
              <option [value]="g.id">
                G{{ g.gen_no }} {{ genState(g.state) }} {{ g.is_current ? '·当前' : '·废弃' }}
                ({{ doneCount(g) }}/{{ g.total_tiles }})
              </option>
            }
          </select>
        </label>
        <label>{{ layerLabel }}
          <select [(ngModel)]="layer">
            @for (n of layerCount; track $index) {
              <option [ngValue]="$index">{{ layerKind }} {{ $index }}</option>
            }
          </select>
        </label>
      </div>

      @if (currentGen(); as g) {
        <div class="metrics">
          <span>切片 {{ doneCount(g) }}/{{ g.total_tiles }}</span>
          <span>重建残差 RMSE：<b>{{ fmt(g.reconstruction_rmse) }}</b></span>
          <span>恢复误差（对照真值）：<b>{{ fmt(g.recovery_rmse) }}</b></span>
          @if (isPartial(g)) { <span class="part">部分结果（{{ failedCount(g) }} 块永久失败）</span> }
        </div>
        <div class="viewer-wrap">
          <app-viewer
            [jobId]="job.id"
            [genId]="g.id"
            [view]="view"
            [layer]="layer"
            [width]="imageWidth"
            [height]="imageHeight"
            [partial]="isPartial(g)"
          ></app-viewer>
        </div>
      }
    } @else {
      <p>加载中…</p>
    }
  `,
  styles: [
    `
      .head { padding: 8px 4px; }
      .status { font-size: 13px; padding: 2px 10px; border-radius: 10px; margin-left: 8px; }
      .status.completed { background: #c8e6c9; }
      .status.running, .status.pending { background: #fff9c4; }
      .status.partial { background: #ffe0b2; }
      .status.canceled, .status.superseded, .status.failed { background: #ffcdd2; }
      .pins { display: flex; gap: 18px; flex-wrap: wrap; font-size: 12px; color: #555; margin: 6px 0; }
      code { background: #f0f0f0; padding: 1px 5px; border-radius: 3px; }
      .warn { background: #ffebee; border-left: 3px solid #c0392b; padding: 6px 10px; margin: 6px 0; font-size: 13px; }
      .actions button { margin-right: 8px; padding: 6px 14px; }
      .pub { background: #1565c0; color: #fff; border: none; border-radius: 4px; }
      .pub:disabled { background: #b0bec5; }
      .pubmsg { margin-top: 8px; font-size: 13px; }
      .ok { color: #2e7d32; }
      .controls { display: flex; gap: 16px; align-items: center; padding: 8px 4px; }
      .seg button { padding: 6px 14px; }
      .seg .on { background: #37474f; color: #fff; }
      .metrics { font-size: 13px; color: #444; display: flex; gap: 20px; padding: 4px; }
      .metrics .part { color: #c0392b; font-weight: bold; }
      .viewer-wrap { height: calc(100vh - 280px); border: 1px solid #ccc; }
    `,
  ],
})
export class JobDetailComponent implements OnInit, OnDestroy {
  job?: JobInfo;
  genId?: number;
  view: View = 'raw';
  layer = 0;
  imageWidth = 1024;
  imageHeight = 1024;
  publishMsg = '';
  publishOk = false;
  private poll?: Subscription;

  constructor(
    private route: ActivatedRoute,
    private api: ApiService,
  ) {}

  ngOnInit(): void {
    const id = +this.route.snapshot.params['id'];
    this.refresh(id);
    this.poll = interval(2000).subscribe(() => this.refresh(id, true));
  }

  ngOnDestroy(): void {
    this.poll?.unsubscribe();
  }

  private refresh(id: number, silent = false): void {
    this.api.job(id).subscribe((j) => {
      const prevGen = this.genId;
      this.job = j;
      if (!this.genId) {
        this.genId = j.generations.find((g) => g.is_current)?.id ?? j.generations[0]?.id;
      }
      if (silent && prevGen !== this.genId) this.genId = prevGen ?? this.genId;
      // width/height come from generation metadata endpoint indirectly;
      // defaults are replaced by querying /images via jobs response if known
      this.imageWidth = (j as unknown as { width?: number }).width ?? 1024;
    });
    this.api.images().subscribe((imgs) => {
      const im = imgs.find((x) => x.id === this.job?.image_id);
      if (im) {
        this.imageWidth = im.width;
        this.imageHeight = im.height;
      }
    });
  }

  get active(): boolean {
    return ['pending', 'running'].includes(this.job?.status ?? '');
  }

  get canRelease(): boolean {
    const g = this.currentGen();
    return !!g && g.is_current && g.state === 'completed';
  }

  currentGen() {
    return this.job?.generations.find((g) => g.id === this.genId);
  }

  doneCount(g: { tiles: Record<string, number> }): number {
    return g.tiles['done'] ?? 0;
  }

  failedCount(g: { tiles: Record<string, number> }): number {
    return g.tiles['failed'] ?? 0;
  }

  isPartial(g: { state: string }): boolean {
    return g.state === 'partial';
  }

  issues(): Array<{ code: string; severity: string; message: string }> {
    return this.job?.frozen?.matrix_issues ?? [];
  }

  maxSaturation(): number {
    const s = this.job?.frozen?.saturation_fraction ?? [];
    return s.length ? Math.max(...s) : 0;
  }

  setView(v: View): void {
    this.view = v;
    this.layer = 0;
  }

  onGen(): void {
    this.layer = 0;
  }

  get layerCount(): number[] {
    if (!this.job) return [0];
    const channels = (this.job.frozen as { display?: { raw_scale: number[] } })
      ?.display?.raw_scale?.length ?? 4;
    const comps = (this.job.frozen as { display?: { comp_scale: number[] } })
      ?.display?.comp_scale?.length ?? 3;
    const n = this.view === 'raw' ? channels : this.view === 'components' ? comps : channels + 1;
    if (this.layer >= n) this.layer = 0;
    return Array(n).fill(0);
  }

  get layerKind(): string {
    return this.view === 'raw' ? '通道' : this.view === 'components' ? '成分' : '残差层';
  }

  get layerLabel(): string {
    return this.view === 'raw'
      ? '检测通道'
      : this.view === 'components'
        ? '荧光成分'
        : '残差（0=RMS，其余为带符号通道残差）';
  }

  fmt(v: number | null | undefined): string {
    return v == null ? '—' : v.toFixed(2);
  }

  statusLabel(s: string): string {
    return {
      pending: '排队', running: '计算中', completed: '已完成', partial: '部分结果',
      canceled: '已取消', superseded: '已废弃（矩阵换版）', failed: '失败',
    }[s] ?? s;
  }

  genState(s: string): string {
    return { running: '计算中', completed: '完整', partial: '部分', canceled: '已取消',
             superseded: '废弃', dispatching: '派发中', abandoned: '废弃' }[s] ?? s;
  }

  cancel(): void {
    if (this.job) this.api.cancelJob(this.job.id).subscribe();
  }

  retry(): void {
    if (this.job)
      this.api.retryJob(this.job.id).subscribe(() => {
        this.genId = undefined;
        this.refresh(this.job!.id);
      });
  }

  publish(): void {
    if (!this.job) return;
    this.api.release(this.job.id).subscribe({
      next: (r) => {
        this.publishOk = true;
        this.publishMsg = `已发布正式结果（报告摘要 ${(r as { report_digest?: string }).report_digest?.slice(0, 16)}…）`;
      },
      error: (e) => {
        this.publishOk = false;
        const d = e.error?.detail;
        this.publishMsg =
          typeof d === 'object' && d ? `发布被拒绝：${d.code} — ${d.detail}` : '发布被拒绝：' + JSON.stringify(d);
      },
    });
  }
}
