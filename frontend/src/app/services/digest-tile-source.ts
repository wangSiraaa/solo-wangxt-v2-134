import OpenSeadragon from 'openseadragon';

export interface DigestTileSourceOptions {
  urlBuilder: (level: number, x: number, y: number) => string;
  width: number;
  height: number;
  tileSize: number;
}

// OpenSeadragon expects level 0 to be the smallest pyramid level. The backend
// stores level 0 as full resolution and maps the incoming level back.
export class DigestTileSource extends OpenSeadragon.TileSource {
  private readonly tileTileSize: number;
  private readonly sourceWidth: number;
  private readonly sourceHeight: number;

  constructor(private readonly opts: DigestTileSourceOptions) {
    const maxLevel = Math.max(
      1,
      Math.ceil(Math.log2(Math.max(opts.width, opts.height) / opts.tileSize)) + 1
    );
    super({
      width: opts.width,
      height: opts.height,
      tileSize: opts.tileSize,
      tileOverlap: 0,
      minLevel: 0,
      maxLevel,
      getTileUrl: (level, x, y) => opts.urlBuilder(level, x, y),
    });
    this.tileTileSize = opts.tileSize;
    this.sourceWidth = opts.width;
    this.sourceHeight = opts.height;
  }

  override getTileUrl(level: number, x: number, y: number): string {
    return this.opts.urlBuilder(level, x, y);
  }

  override tileExists(level: number, x: number, y: number): boolean {
    const scale = this.getLevelScale(level);
    const width = Math.ceil(this.sourceWidth * scale);
    const height = Math.ceil(this.sourceHeight * scale);
    const cols = Math.ceil(width / this.tileTileSize);
    const rows = Math.ceil(height / this.tileTileSize);
    return x >= 0 && y >= 0 && x < cols && y < rows;
  }
}
