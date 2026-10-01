import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Component, OnDestroy, OnInit, ViewChild } from '@angular/core';
import OpenSeadragon from 'openseadragon';
import { ApiService, ImageInfo, JobSnapshot, MatrixVersion, TileInfo } from './services/api.service';
import { LinkedViewerComponent, ViewerInput } from './linked-viewer.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, FormsModule, LinkedViewerComponent],
  template: `
    <main class="shell">
      <header class="header">
        <div>
          <h1>荧光通道解混平台</h1>
          <div class="muted">原通道 · 估计成分 · 重建残差；缓存与发布均由 图像/矩阵/算法/冻结统计/切片层级 绑定。</div>
        </div>
        <div>
          <select [(ngModel)]="loginEmail">
            <option value="viewer@example.com">viewer：仅看原图</option>
            <option value="engineer@example.com">engineer：发布矩阵/计算</option>
            <option value="publisher@example.com">publisher：发布结果</option>
            <option value="admin@example.com">admin</option>
          </select>
          <button (click)="login()">登录</button>
          <button (click)="bootstrapDemo()">初始化演示数据</button>
        </div>
      </header>

      <div *ngIf="error" class="notice error">{{ error }}</div>
      <div *ngIf="notice" class="notice warn">{{ notice }}</div>

      <section class="controls">
        <label class="field">图像
          <select [(ngModel)]="selectedImageId">
            <option *ngFor="let image of images" [value]="image.id">{{ image.name }} ({{ image.width }}×{{ image.height }})</option>
          </select>
        </label>
        <label class="field">矩阵版本
          <select [(ngModel)]="selectedMatrixId">
            <option *ngFor="let m of matrices" [value]="m.id">
              v{{m.version_no}} {{m.name}} · {{m.status}} · {{m.coefficient_digest.slice(0,10)}}
            </option>
          </select>
        </label>
        <label class="field">算法
          <select [(ngModel)]="algorithm">
            <option value="nnls">SciPy NNLS（精确参考）</option>
            <option value="fista_nnls">FISTA NNLS（向量化）</option>
          </select>
        </label>
        <label class="field">预览通道/成分
          <input type="number" min="0" [(ngModel)]="channelIndex">
        </label>
        <button class="primary" (click)="createJob()">新计算</button>
        <button (click)="refreshJob()">刷新</button>
        <button class="danger" (click)="cancelJob()">取消当前</button>
        <button (click)="retryJob()">取消后重试</button>
        <button class="primary" (click)="publish()">正式发布</button>
        <button (click)="newMatrixVersion()">模拟计算中换版</button>
      </section>

      <section *ngIf="snapshot">
        <h2>任务状态</h2>
        <p>
          状态 <span class="badge" [class.ok]="snapshot.status==='succeeded'"
                        [class.partial]="snapshot.status==='partial'"
                        [class.stale]="snapshot.status==='cancelled'">{{ snapshot.status }}</span>
          · 当前代次 <b>g{{ snapshot.generation_no }}</b>
          · 冻结摘要 <code>{{ snapshot.frozen_digest }}</code>
          · 必需切片 {{ snapshot.required_tile_count }}
        </p>
        <div *ngIf="snapshot.status==='partial'" class="notice warn">
          预览为明确标记的部分结果：缺失或失败切片不会用旧矩阵/旧代次补洞；正式发布被拒绝。
        </div>
        <div *ngIf="snapshot.quality_flags as q">
          饱和切片：{{ q['saturated_tiles'] || 0 }}；永久失败切片：{{ q['failed_tiles'] || 0 }}
        </div>
        <div class="tile-grid" title="绿色=成功，灰色=等待/运行，红色=单块损坏，紫=废弃/取消">
          <span *ngFor="let tile of tiles" class="tile" [class]="tile.kind + ' ' + tile.status"
                [title]="tile.kind + ' l'+tile.level+' ('+tile.x+','+tile.y+') '+tile.status+' attempts='+tile.attempts+' '+(tile.error_code||'')"></span>
        </div>
      </section>

      <section *ngIf="viewersReady" class="viewer-grid">
        <app-linked-viewer #sourceView title="原通道（不可变）" badgeKind="info" badge="source digest"
          [source]="sourceViewer!" (viewportChanged)="sync($event, 'source')"></app-linked-viewer>
        <app-linked-viewer #componentView title="估计成分"
          [badge]="'g'+(snapshot?.generation_no||0)+' matrix '+matrixDigest.slice(0,10)"
          [badgeKind]="snapshot?.status==='partial' ? 'partial' : 'info'"
          [source]="componentViewer!" (viewportChanged)="sync($event, 'component')"></app-linked-viewer>
        <app-linked-viewer #residualView title="重建残差"
          [badge]="'frozen '+(snapshot?.frozen_digest||'').slice(0,10)"
          [badgeKind]="snapshot?.status==='partial' ? 'partial' : 'info'"
          [source]="residualViewer!" (viewportChanged)="sync($event, 'residual')"></app-linked-viewer>
      </section>

      <section *ngIf="failedTiles.length">
        <h2>损坏/失败记录</h2>
        <table>
          <tr><th>层级</th><th>坐标</th><th>视图</th><th>尝试</th><th>错误</th></tr>
          <tr *ngFor="let t of failedTiles">
            <td>{{t.level}}</td><td>({{t.x}}, {{t.y}})</td><td>{{t.kind}}</td><td>{{t.attempts}}</td>
            <td>{{t.error_code}}</td>
          </tr>
        </table>
      </section>
    </main>
  `,
})
export class AppComponent implements OnInit, OnDestroy {
  @ViewChild(LinkedViewerComponent) sourceView?: LinkedViewerComponent;
  @ViewChild('componentView') componentView?: LinkedViewerComponent;
  @ViewChild('residualView') residualView?: LinkedViewerComponent;
  images: ImageInfo[] = [];
  matrices: MatrixVersion[] = [];
  tiles: TileInfo[] = [];
  snapshot?: JobSnapshot;
  selectedImageId = '';
  selectedMatrixId = '';
  loginEmail = 'engineer@example.com';
  algorithm = 'fista_nnls';
  channelIndex = 0;
  jobId = '';
  error = '';
  notice = '';
  matrixDigest = '';
  sourceViewer?: ViewerInput;
  componentViewer?: ViewerInput;
  residualViewer?: ViewerInput;
  viewersReady = false;
  private timer?: ReturnType<typeof setInterval>;

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    const token = localStorage.getItem('unmix.token');
    if (token) this.loadInitial();
  }
  ngOnDestroy(): void { if (this.timer) clearInterval(this.timer); }

  login(): void {
    const password = this.loginEmail.split('@')[0] + '-password';
    this.api.login(this.loginEmail, password).subscribe({
      next: (res) => {
        localStorage.setItem('unmix.token', res.access_token);
        localStorage.setItem('unmix.role', res.role);
        this.error = '';
        this.loadInitial();
      },
      error: (e) => this.showError(e),
    });
  }

  bootstrapDemo(): void {
    this.api.bootstrap().subscribe({ next: () => this.loadInitial(), error: (e) => this.showError(e) });
  }

  loadInitial(): void {
    this.api.images().subscribe((images) => {
      this.images = images;
      if (!this.selectedImageId && images[0]) this.selectedImageId = images[0].id;
    });
    this.api.matrices().subscribe((matrices) => {
      this.matrices = matrices;
      const published = matrices.find((m) => m.status === 'published') || matrices[0];
      if (published) {
        this.selectedMatrixId = published.id;
        this.matrixDigest = published.coefficient_digest;
      }
    });
  }

  createJob(): void {
    this.viewersReady = false;
    this.api.createJob({
      image_id: this.selectedImageId,
      matrix_version_id: this.selectedMatrixId,
      algorithm: this.algorithm,
    }).subscribe({
      next: (job) => {
        this.jobId = job.id;
        this.notice = '任务创建时已固定图像与矩阵摘要；整图统计先冻结后分块。';
        this.startPolling();
      },
      error: (e) => this.showError(e),
    });
  }

  startPolling(): void {
    if (this.timer) clearInterval(this.timer);
    this.refreshJob();
    this.timer = setInterval(() => this.refreshJob(false), 1200);
  }

  refreshJob(showErrors = true): void {
    if (!this.jobId) return;
    this.api.job(this.jobId).subscribe({
      next: (snapshot) => {
        const oldGen = this.snapshot?.generation_no;
        this.snapshot = snapshot;
        this.api.tiles(this.jobId).subscribe((tiles) => this.tiles = tiles);
        if (!this.viewersReady) this.buildViewers();
        if (oldGen && oldGen !== snapshot.generation_no) {
          this.notice = `检测到新代次 g${snapshot.generation_no}：旧任务迟到输出仅进入废弃代次，当前视图不会拼接旧切片。`;
        }
      },
      error: (e) => showErrors && this.showError(e),
    });
  }

  buildViewers(): void {
    const image = this.images.find((i) => i.id === this.selectedImageId)!;
    const size = 1024;
    this.sourceViewer = {
      width: image.width, height: image.height, tileSize: size,
      urlBuilder: (l, x, y) => this.api.tileUrl(image.id, l, x, y, this.channelIndex),
      badge: image.id,
    };
    this.componentViewer = {
      width: image.width, height: image.height, tileSize: size,
      urlBuilder: (l, x, y) => this.api.jobTileUrl(this.jobId, 'component', l, x, y, this.channelIndex),
      badge: '',
    };
    this.residualViewer = {
      width: image.width, height: image.height, tileSize: size,
      urlBuilder: (l, x, y) => this.api.jobTileUrl(this.jobId, 'residual', l, x, y, 0),
      badge: '',
    };
    this.viewersReady = true;
  }

  sync(viewport: OpenSeadragon.Viewport, origin: string): void {
    if (origin !== 'source') this.sourceView?.applyViewport(viewport);
    if (origin !== 'component') this.componentView?.applyViewport(viewport);
    if (origin !== 'residual') this.residualView?.applyViewport(viewport);
  }

  cancelJob(): void { this.api.cancel(this.jobId).subscribe({ next: () => this.refreshJob(), error: (e) => this.showError(e) }); }
  retryJob(): void {
    this.api.retry(this.jobId).subscribe({
      next: () => { this.notice = '重试创建新一代，旧代次全部作废；成功切片仅在摘要完全相同时可按缓存键复用。'; this.startPolling(); },
      error: (e) => this.showError(e),
    });
  }
  publish(): void {
    this.api.publish(this.jobId).subscribe({
      next: () => this.notice = '发布成功：报告清单逐块核对同代次、同摘要；审计已记录。',
      error: (e) => this.showError(e),
    });
  }

  newMatrixVersion(): void {
    const old = this.matrices.find((m) => m.id === this.selectedMatrixId);
    if (!old) return;
    const perturb = old.coefficients.map((row) => row.map((v) => Math.max(0, v * (1 + (Math.random() - .5) * .04))));
    this.api.createMatrix({
      name: old.name + ' mid-flight',
      matrix_key: old.matrix_key,
      channel_names: old.channel_names,
      component_names: old.component_names,
      coefficients: perturb,
      publish: true,
      allow_duplicate_version: true,
    }).subscribe({
      next: (m) => {
        this.loadInitial();
        this.notice = `矩阵新版本 v${m.version_no} 已发布；进行中的任务仍绑定旧摘要，新任务才使用新矩阵。`;
      },
      error: (e) => this.showError(e),
    });
  }

  get failedTiles(): TileInfo[] { return this.tiles.filter((t) => t.status === 'failed'); }

  private showError(e: any): void {
    this.error = e?.error?.detail || e?.error?.code || e?.message || '请求失败';
    if (e?.error) this.error += '（' + (e.error.code || '') + '）';
  }
}
