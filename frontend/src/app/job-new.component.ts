import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';
import { ApiService } from './api.service';
import { MatrixInfo } from './models';

@Component({
  selector: 'app-job-new',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <h2>新建解混任务</h2>
    <p>图像 ID：{{ imageId }} — 启动时将固定图像与矩阵的内容摘要。</p>
    <label>串色矩阵版本
      <select [(ngModel)]="matrixId">
        @for (m of matrices; track m.id) {
          <option [value]="m.id">
            {{ m.name }} v{{ m.version }} {{ m.superseded ? '(已停用)' : '' }} · {{ m.digest.slice(0, 12) }}
          </option>
        }
      </select>
    </label>
    <button (click)="submit()">启动</button>
    @if (error) { <p class="err">{{ error }}</p> }
  `,
  styles: `select{display:block;width:420px;padding:8px;margin:8px 0;} .err{color:#c0392b;}`,
})
export class JobNewComponent implements OnInit {
  imageId!: number;
  matrices: MatrixInfo[] = [];
  matrixId?: number;
  error = '';

  constructor(
    private route: ActivatedRoute,
    private router: Router,
    private api: ApiService,
  ) {}

  ngOnInit(): void {
    this.imageId = +this.route.snapshot.params['imageId'];
    this.api.matrices().subscribe((ms) => {
      this.matrices = ms;
      const current = [...ms].reverse().find((m) => !m.superseded);
      this.matrixId = current?.id;
    });
  }

  submit(): void {
    this.api
      .createJob(this.imageId, this.matrixId)
      .subscribe({
        next: (j) => this.router.navigate(['/jobs', j.id]),
        error: (e) =>
          (this.error = e.error?.detail ?? '无法创建任务（权限或参数问题）'),
      });
  }
}
