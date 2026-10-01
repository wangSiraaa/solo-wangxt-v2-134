import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from './api.service';
import { MatrixInfo } from './models';

@Component({
  selector: 'app-matrix-admin',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <h2>串色矩阵版本</h2>
    <p class="note">
      发布新版本会使所有<strong>进行中</strong>的同矩阵任务立即进入废弃代次并自动创建新代次后继；
      旧版本字节按内容摘要永久保留，已发布报告不受影响。每次修订都写入审计。
    </p>
    <table>
      <tr><th>ID</th><th>名称</th><th>版本</th><th>形状</th><th>摘要</th><th>状态</th><th>备注</th></tr>
      @for (m of matrices; track m.id) {
        <tr [class.old]="m.superseded">
          <td>{{ m.id }}</td><td>{{ m.name }}</td><td>v{{ m.version }}</td>
          <td>{{ m.shape[0] }}×{{ m.shape[1] }}</td>
          <td class="digest">{{ m.digest.slice(0, 16) }}…</td>
          <td>{{ m.superseded ? '已停用' : '当前' }}</td>
          <td>{{ m.note }}</td>
        </tr>
      }
    </table>

    <div class="pub-box">
      <h3>发布修订（需要 matrix:publish 能力）</h3>
      <input [(ngModel)]="name" placeholder="矩阵名称（如 default）" />
      <textarea [(ngModel)]="matrixText" rows="6"
        placeholder="每行一个检测通道，列对应荧光成分，例如&#10;[[0.88,0.20,0.02],[0.22,0.82,0.05],[0.02,0.22,0.88],[0.01,0.02,0.30]]"></textarea>
      <button (click)="publish()">{{ busy ? '发布并失效旧代次…' : '发布新版本' }}</button>
      @if (msg) { <p [class.ok]="ok" [class.err]="!ok">{{ msg }}</p> }
    </div>
  `,
  styles: [
    `
      table { border-collapse: collapse; width: 100%; }
      td, th { border: 1px solid #e0e0e0; padding: 6px 10px; font-size: 13px; }
      tr.old td { color: #aaa; }
      .digest { font-family: monospace; }
      .note { background: #fff8e1; padding: 8px 12px; border-left: 3px solid #f9a825; font-size: 13px; }
      .pub-box { margin-top: 20px; background: #f7f7f7; padding: 14px; border-radius: 8px; }
      input, textarea { width: 100%; box-sizing: border-box; padding: 8px; margin: 6px 0; font-family: monospace; }
      .ok { color: #2e7d32; } .err { color: #c0392b; }
    `,
  ],
})
export class MatrixAdminComponent implements OnInit {
  matrices: MatrixInfo[] = [];
  name = 'default';
  matrixText =
    '[[0.88,0.20,0.02],[0.22,0.82,0.05],[0.02,0.22,0.88],[0.01,0.02,0.30]]';
  busy = false;
  msg = '';
  ok = false;

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.api.matrices().subscribe((ms) => (this.matrices = ms));
  }

  publish(): void {
    let matrix: number[][];
    try {
      matrix = JSON.parse(this.matrixText);
      if (!Array.isArray(matrix) || !matrix.every((r) => Array.isArray(r)))
        throw new Error('shape');
    } catch {
      this.ok = false;
      this.msg = '矩阵必须是二维 JSON 数组';
      return;
    }
    this.busy = true;
    this.api.publishMatrix(this.name, matrix).subscribe({
      next: (m) => {
        this.busy = false;
        this.ok = true;
        this.msg = `已发布 ${m.name} v${m.version}（${m.digest.slice(0, 16)}…），旧代次任务已围栏`;
        this.refresh();
      },
      error: (e) => {
        this.busy = false;
        this.ok = false;
        this.msg = '发布失败：' + JSON.stringify(e.error?.detail ?? e.message);
      },
    });
  }
}
