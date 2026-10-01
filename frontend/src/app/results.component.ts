import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';
import { ApiService } from './api.service';
import { ResultInfo } from './models';

@Component({
  selector: 'app-results',
  standalone: true,
  imports: [CommonModule, RouterLink],
  template: `
    <h2>已发布结果版本（不可变报告）</h2>
    <table>
      <tr><th>ID</th><th>任务</th><th>代次</th><th>恢复 RMSE</th><th>重建 RMSE</th><th>报告摘要</th><th></th></tr>
      @for (r of results; track r.id) {
        <tr>
          <td>{{ r.id }}</td>
          <td><a [routerLink]="['/jobs', r.job_id]">#{{ r.job_id }}</a></td>
          <td>G{{ r.generation_id }}</td>
          <td>{{ r.recovery_rmse?.toFixed(2) ?? '—' }}</td>
          <td>{{ r.reconstruction_rmse?.toFixed(2) ?? '—' }}</td>
          <td class="digest">{{ r.report_digest.slice(0, 16) }}…</td>
          <td><a [routerLink]="['/jobs', r.job_id]">查看联动切片</a></td>
        </tr>
      }
    </table>
  `,
  styles: `table{border-collapse:collapse;width:100%;}td,th{border:1px solid #e0e0e0;padding:6px 10px;font-size:13px;} .digest{font-family:monospace;}`,
})
export class ResultsComponent implements OnInit {
  results: ResultInfo[] = [];
  constructor(private api: ApiService) {}
  ngOnInit(): void {
    this.api.results().subscribe((rs) => (this.results = rs));
  }
}
