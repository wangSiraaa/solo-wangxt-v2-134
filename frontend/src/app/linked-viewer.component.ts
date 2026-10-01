import { AfterViewInit, Component, ElementRef, EventEmitter, Input, OnDestroy, Output, ViewChild } from '@angular/core';
import OpenSeadragon from 'openseadragon';
import { DigestTileSource } from './services/digest-tile-source';

export interface ViewerInput {
  width: number;
  height: number;
  tileSize: number;
  urlBuilder: (level: number, x: number, y: number) => string;
  badge: string;
}

@Component({
  selector: 'app-linked-viewer',
  standalone: true,
  template: `
    <div class="viewer-card">
      <div class="viewer-title">
        <span>{{ title }}</span>
        <span class="badge" [class.ok]="badgeKind==='ok'" [class.info]="badgeKind==='info'" [class.partial]="badgeKind==='partial'">{{ badge }}</span>
      </div>
      <div #viewer class="viewer"></div>
    </div>
  `,
})
export class LinkedViewerComponent implements AfterViewInit, OnDestroy {
  @Input() title = '';
  @Input() badge = '';
  @Input() badgeKind: 'ok' | 'info' | 'partial' = 'info';
  @Input() source!: ViewerInput;
  @Output() viewportChanged = new EventEmitter<OpenSeadragon.Viewport>();
  @ViewChild('viewer') viewerElement!: ElementRef<HTMLDivElement>;
  private viewer?: OpenSeadragon.Viewport & any;
  private syncing = false;

  ngAfterViewInit(): void {
    const tileSource = new DigestTileSource({
      width: this.source.width,
      height: this.source.height,
      tileSize: this.source.tileSize,
      urlBuilder: this.source.urlBuilder,
    });
    this.viewer = OpenSeadragon({
      element: this.viewerElement.nativeElement,
      tileSources: [tileSource],
      prefixUrl: 'https://cdn.jsdelivr.net/npm/openseadragon@5.0.1/build/openseadragon/images/',
      showNavigationControl: true,
      navigationControlAnchor: OpenSeadragon.ControlAnchor.TOP_LEFT,
      gestureSettingsMouse: { clickToZoom: false, scrollToZoom: true },
      crossOriginPolicy: 'Anonymous',
    }) as any;
    this.viewer.addHandler('viewport-change', () => {
      if (!this.syncing) this.viewportChanged.emit(this.viewer.viewport);
    });
  }

  applyViewport(viewport: OpenSeadragon.Viewport): void {
    if (!this.viewer || this.syncing) return;
    this.syncing = true;
    const target = this.viewer.viewport as OpenSeadragon.Viewport;
    target.zoomTo(viewport.getZoom(), undefined, false);
    target.panTo(viewport.getCenter(), false);
    target.update();
    requestAnimationFrame(() => (this.syncing = false));
  }

  ngOnDestroy(): void {
    this.viewer?.destroy?.();
  }
}
