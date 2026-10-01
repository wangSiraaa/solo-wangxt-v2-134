import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { ApiService } from './api.service';
import { ImageInfo } from './models';

@Component({
  selector: 'app-image-list',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  template: `
    <h2>图像（多通道，内容寻址不可变）</h2>
    <div class="gen">
      <label>生成已知组分合成图（含真值，用于核对恢复误差）</label>
      <div class="row">
        <input [(ngModel)]="name" placeholder="名称" />
        <input type="number" [(ngModel)]="h" min="64" max="24000" placeholder="高" />
        <input type="number" [(ngModel)]="w" min="64" max="24000" placeholder="宽" />
        <button (click)="generate()" [disabled]="busy">{{ busy ? '生成中…' : '生成 1024² 合成图' }}</button>
      </div>
      @if (note) { <p class="ok">{{ note }}</p> }
    </div>
    <table>
      <tr><th>ID</th><th>名称</th><th>尺寸</th><th>通道</th><th>摘要</th><th>权限</th><th></th></tr>
      @for (im of images; track im.id) {
        <tr>
          <td>{{ im.id }}</td>
          <td>{{ im.name }} @if (im.has_ground_truth) { <span class="tag gt">真值</span> }</td>
          <td>{{ im.width }}×{{ im.height }}</td>
          <td>{{ im.channels }}</td>
          <td class="digest">{{ im.digest.slice(0, 16) }}…</td>
          <td>
            @if (im.can_view) { <span class="tag view">可查看</span> }
            @if (im.can_publish) { <span class="tag pub">可发布</span> }
          </td>
          <td>
            @if (im.can_view) {
              <a [routerLink]="['/jobs/new', im.id]">新建任务</a>
            } @else { <em>无查看权限</em> }
          </td>
        </tr>
      }
    </table>
  `,
  styles: [
    `
      table { border-collapse: collapse; width: 100%; margin-top: 16px; }
      td, th { border: 1px solid #e0e0e0; padding: 6px 10px; text-align: left; font-size: 13px; }
      .digest { font-family: monospace; color: #666; }
      .tag { padding: 1px 7px; border-radius: 10px; font-size: 11px; margin-right: 4px; }
      .gt { background: #eee; } .view { background: #dcedc8; } .pub { background: #ffe0b2; }
      .gen { background: #f7f7f7; padding: 12px; border-radius: 8px; margin: 12px 0; }
      .row { display: flex; gap: 8px; margin-top: 8px; }
      .row input { width: 110px; padding: 6px; }
      .ok { color: #2e7d32; }
    `,
  ],
})
export class ImageListComponent implements OnInit {
  images: ImageInfo[] = [];
  name = 'synthetic';
  h = 1024;
  w = 1024;
  busy = false;
  note = '';

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.api.images().subscribe((xs) => (this.images = xs));
  }

  generate(): void {
    this.busy = true;
    this.api
      .createSynthetic(this.name, this.h, this.w)
      .subscribe({
        next: () => {
          this.busy = false;
          this.note = '合成图已生成（原图与真值均为内容寻址对象）';
          this.refresh();
        },
        error: () => (this.busy = false),
      });
  }
}
