export interface Me {
  id: number;
  name: string;
  scopes: string[];
}

export interface ImageInfo {
  id: number;
  name: string;
  width: number;
  height: number;
  channels: number;
  digest: string;
  has_ground_truth: boolean;
  can_view: boolean;
  can_publish: boolean;
}

export interface GenerationInfo {
  id: number;
  gen_no: number;
  state: string;
  is_current: boolean;
  tiles: Record<string, number>;
  total_tiles: number;
  recovery_rmse: number | null;
  reconstruction_rmse: number | null;
  metrics: Record<string, unknown> | null;
}

export interface JobInfo {
  id: number;
  image_id: number;
  matrix_version_id: number;
  algorithm: string;
  image_digest: string;
  matrix_digest: string;
  status: string;
  params_hash: string | null;
  error_code: string | null;
  error_detail: string | null;
  successor_job_id: number | null;
  generations: GenerationInfo[];
  frozen?: {
    display: {
      raw_scale: number[];
      comp_scale: number[];
      resid_scale: number;
    };
    saturation_fraction: number[];
    matrix_issues?: Array<{ code: string; severity: string; message: string }>;
  };
}

export interface MatrixInfo {
  id: number;
  name: string;
  version: number;
  digest: string;
  superseded: boolean;
  note: string | null;
  shape: [number, number];
}

export interface ResultInfo {
  id: number;
  job_id: number;
  generation_id: number;
  is_current: boolean;
  report_digest: string;
  report_object_key: string;
  recovery_rmse: number | null;
  reconstruction_rmse: number | null;
}
