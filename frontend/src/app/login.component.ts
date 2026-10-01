import { Component, OnInit } from '@angular/core';
import { ApiService } from './api.service';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { RouterLink } from '@angular/router';

@Component({
  selector: 'app-login',
  standalone: true,
  imports: [FormsModule, CommonModule, RouterLink],
  template: `
    <div class="wrap">
      <h1>荧光通道解混平台</h1>
      <p class="sub">Fluorescence Unmixing · 金字塔切片联动</p>
      <label>API Key
        <input [(ngModel)]="key" placeholder="粘贴 API Key" (keyup.enter)="save()" />
      </label>
      <button (click)="save()">进入</button>
      @if (error) { <p class="err">{{ error }}</p> }
      <p class="hint">
        预置：imager-key · analyst-key · matrixer-key · publisher-key · admin-key
      </p>
    </div>
  `,
  styles: [
    `
      .wrap { max-width: 460px; margin: 10vh auto; padding: 28px; border: 1px solid #ddd; border-radius: 10px; }
      input { width: 100%; padding: 10px; margin: 8px 0; font-family: monospace; }
      button { padding: 10px 22px; }
      .err { color: #c0392b; }
      .hint { color: #888; font-size: 12px; }
    `,
  ],
})
export class LoginComponent implements OnInit {
  key = '';
  error = '';

  constructor(public api: ApiService) {}

  ngOnInit(): void {
    this.key = this.api.apiKey();
  }

  save(): void {
    this.api.apiKey.set(this.key.trim());
    this.api.me().subscribe({
      next: () => (location.hash = '#/images'),
      error: () => (this.error = '无效的 API Key'),
    });
  }
}
