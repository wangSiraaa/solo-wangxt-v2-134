import { Component } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { CommonModule } from '@angular/common';
import { ApiService } from './api.service';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, RouterOutlet, RouterLink, RouterLinkActive],
  template: `
    @if (api.apiKey(); as key) {
      <nav>
        <span class="brand">🔬 荧光解混</span>
        <a routerLink="/images" routerLinkActive="on">图像</a>
        <a routerLink="/matrices" routerLinkActive="on">矩阵版本</a>
        <a routerLink="/results" routerLinkActive="on">已发布结果</a>
        <span class="spacer"></span>
        <span class="key">{{ key.slice(0, 12) }}…</span>
        <button class="logout" (click)="logout()">退出</button>
      </nav>
    }
    <main><router-outlet /></main>
  `,
  styles: [
    `
      nav { display: flex; align-items: center; gap: 14px; padding: 10px 18px; background: #263238; color: #fff; }
      nav a { color: #cfd8dc; text-decoration: none; font-size: 14px; }
      nav a.on { color: #fff; font-weight: bold; border-bottom: 2px solid #4dd0e1; }
      .brand { font-weight: bold; margin-right: 12px; }
      .spacer { flex: 1; }
      .key { font-family: monospace; font-size: 12px; color: #90a4ae; }
      .logout { background: none; border: 1px solid #78909c; color: #fff; border-radius: 4px; padding: 3px 10px; cursor: pointer; }
      main { padding: 14px 20px; }
    `,
  ],
})
export class AppComponent {
  constructor(public api: ApiService) {}
  logout(): void {
    this.api.apiKey.set('');
    location.hash = '#/login';
  }
}
