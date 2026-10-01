import {
  AfterViewInit,
  Component,
  ElementRef,
  Input,
  OnChanges,
  OnDestroy,
  ViewChild,
} from '@angular/core';
import OpenSeadragon from 'openseadragon';
import { ApiService } from '../api.service';

/**
 * Minimal custom tile source. We do NOT fetch the .dzi XML client-side: the
 * pyramid geometry (tileSize/levels) is known, so we only need getTileUrl.
 * URLs pin job+generation+view+layer, which makes the browser cache fail
 * atomically on a matrix revision.
 */
class PinnedTileSource extends OpenSeadragon.TileSource {
  constructor(
    width: number,
    height: number,
    tileSize: number,
    maxLevel: number,
    private readonly buildUrl: (level: number, x: number, y: number) => string,
  ) {
    super({ width, height, tileSize, minLevel: 0, maxLevel });
  }

  override getTileUrl(level: number, x: number, y: number): string {
    return this.buildUrl(level, x, y);
  }
}

@Component({
  selector: 'app-viewer',
  standalone: true,
  host: { '[class.partial]': 'partial' },
  template: `
    <div #viewport class="viewport"></div>
    @if (partial) {
      <div class="partial-banner">⚠ 部分结果预览（PARTIAL）— 缺失/失败切片已标识，非正式代次，不可发布</div>
    }
  `,
  styles: [
    `
      .viewport { width: 100%; height: 100%; background: #111; outline-offset: -3px; }
      :host(.partial) .viewport { outline: 3px dashed #c0392b; }
      .partial-banner {
        position: absolute; top: 8px; left: 8px; background: #c0392b; color: #fff;
        padding: 4px 10px; font-size: 12px; border-radius: 4px; z-index: 5;
      }
      :host { position: relative; display: block; height: 100%; }
    `,
  ],
})
export class ViewerComponent implements OnChanges, OnDestroy, AfterViewInit {
  @Input({ required: true }) jobId!: number;
  @Input({ required: true }) genId!: number;
  @Input({ required: true }) view!: 'raw' | 'components' | 'residual';
  @Input() layer = 0;
  @Input() width = 1024;
  @Input() height = 1024;
  @Input() tileSize = 512;
  @Input() partial = false;

  @ViewChild('viewport', { static: true }) el!: ElementRef<HTMLDivElement>;
  private viewer?: OpenSeadragon.Viewer;

  constructor(private api: ApiService) {}

  ngAfterViewInit(): void {
    this.mount();
  }

  ngOnChanges(): void {
    if (this.viewer) {
      this.viewer.destroy();
      this.viewer = undefined;
    }
    if (this.el) this.mount();
  }

  private mount(): void {
    // OSD level 0 = coarsest single tile; our backend L0 is full resolution,
    // and the tile endpoint maps dzi_level -> internal level accordingly.
    const levels =
      Math.max(
        1,
        Math.ceil(
          Math.log2(Math.max(this.width, this.height) / this.tileSize),
        ) + 1,
      ) - 1;
    const source = new PinnedTileSource(
      this.width,
      this.height,
      this.tileSize,
      levels,
      (lvl, x, y) =>
        `${this.api.baseUrl}/viewer/jobs/${this.jobId}/generations/${this.genId}/` +
        `${this.view}_files/${lvl}/${x}_${y}.png?layer=${this.layer}`,
    );

    this.viewer = OpenSeadragon({
      element: this.el.nativeElement,
      prefixUrl:
        'https://cdn.jsdelivr.net/npm/openseadragon@4.1.0/build/openseadragon/images/',
      showNavigationControl: true,
      gestureSettingsMouse: { clickToZoom: false },
      // Credentials travel in a header (never in the cacheable URL); every
      // tile request is XHR so the key can be attached.
      loadTilesWithAjax: true,
      ajaxHeaders: { 'X-API-Key': this.api.apiKey() },
      tileSources: source as unknown as OpenSeadragon.TileSourceOptions,
    });
  }

  ngOnDestroy(): void {
    this.viewer?.destroy();
  }
}
