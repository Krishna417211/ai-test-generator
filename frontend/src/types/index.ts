// `User` is owned by api.ts (it mirrors the server's public_user shape). Import
// it type-only so this stays erased at build time and no import cycle forms.
import type { User } from "../utils/api";

export type Framework = "playwright" | "cypress" | "selenium";
export type Language = "typescript" | "javascript" | "python" | "java";

export interface ProjectAnalysis {
  project_summary: string;
  framework: string;
  key_pages: KeyPage[];
  key_components: KeyComponent[];
  routes: string[];
  testing_challenges: string[];
  file_count: number;
  total_tokens: number;
  file_previews: FilePreview[];
}

export interface KeyPage {
  path: string;
  description: string;
  test_priority: "high" | "medium" | "low";
  suggested_tests: string[];
}

export interface KeyComponent {
  path: string;
  description: string;
}

export interface FilePreview {
  path: string;
  size: number;
  importance: number;
  preview: string;
}

export interface GeneratedFile {
  filename: string;
  content: string;
  description: string;
  size: number;
}

export interface FileValidation {
  filename: string;
  ok: boolean;
  error: string;
}

export interface GenerateResponse {
  success: boolean;
  files: GeneratedFile[];
  test_count: number;
  framework: string;
  selector_warnings: string[];
  summary: string;
  validation?: FileValidation[];
  error?: string;
}

export interface PublishResult {
  success: boolean;
  repo_url: string;
  full_name: string;
  branch: string;
  commit_sha: string;
  files_pushed: number;
  cicd_added: boolean;
  test_count: number;
  all_valid: boolean;
  validation?: FileValidation[];
  warnings: string[];
}

export type Severity = "critical" | "high" | "medium" | "low" | "info";

export interface SecurityFinding {
  severity: Severity;
  category: string;
  title: string;
  description: string;
  remediation: string;
  evidence?: string;
  /** A YouTube *search* for fixing this issue, built server-side from a fixed
   *  query (see security_scanner._youtube). Never a specific video id and never
   *  model-generated, so it can't be a fabricated or dead link. */
  video_url?: string;
}

export interface ScanResult {
  url: string;
  final_url: string;
  score: number;
  grade: string;
  summary: string;
  counts: Record<string, number>;
  checks_run: number;
  findings: SecurityFinding[];
}

export interface ProviderStatus {
  name: string;
  total_keys: number;
  available_keys: number;
  total_calls: number;
  healthy: boolean;
}

// ── Profile & dashboard ──────────────────────

export interface GenerationRecord {
  created_at: number;
  source: string;
  framework: string;
  language: string;
  test_count: number;
  file_count: number;
  /** "success" | "failed" — failed runs are recorded too, so the history doesn't
   *  silently omit the generation the user watched fall over. */
  status: string;
  duration_ms: number | null;
  error: string | null;
}

export interface ScanRecord {
  created_at: number;
  url: string;
  grade: string;
  score: number;
  findings: number;
  duration_ms: number | null;
  /** Severity breakdown, e.g. { high: 1, medium: 3 }. Empty for older rows. */
  counts: Record<string, number>;
}

export interface PublishedRepo {
  full_name: string;
  repo_url: string;
  created_at: number;
  test_count: number;
  files_pushed: number;
  cicd_added: boolean;
  private: boolean;
}

export type ActivityKind = "generate" | "publish" | "scan";

/** One day's per-feature counts. Zero-filled by the server, so a flat spot in
 *  the chart is a real quiet day rather than a missing row. */
export interface ActivityDay {
  date: string;                 // YYYY-MM-DD
  generate: number;
  publish: number;
  scan: number;
}

/** A row in the unified feed. The server folds the three activity tables into
 *  one timeline; `kind` discriminates which fields are present. */
export type ActivityItem =
  | ({ kind: "generate"; created_at: number; status: string } & Pick<
      GenerationRecord, "source" | "framework" | "test_count" | "file_count">)
  | ({ kind: "publish"; created_at: number; status: string } & Pick<
      PublishedRepo, "full_name" | "repo_url" | "test_count" | "files_pushed">)
  | ({ kind: "scan"; created_at: number; status: string } & Pick<
      ScanRecord, "url" | "grade" | "score" | "findings">);

export interface UserSettings {
  framework: string;
  language: string;
  base_url: string;
}

export interface PlanCatalogueEntry {
  id: "monthly" | "yearly";
  name: string;
  price_usd: number;
  interval: string;
  checkout_url: string | null;
  savings_usd?: number;
}

export interface Profile {
  user: User;
  member_since: number | null;
  plan: {
    plan: string;
    used: number;
    limit: number | null;
    remaining: number | null;
    period: string;
    resets_at: number;
    /** Only set for a live paid plan; free plans never carry a renewal date. */
    expires_at: number | null;
    checkout_available: boolean;
    catalogue: PlanCatalogueEntry[];
  };
  totals: {
    generations: number;
    tests_written: number;
    /** Lifetime failed generations. Counted inside `generations`, not on top. */
    generations_failed: number;
    scans: number;
    repos_published: number;
  };
  generations: GenerationRecord[];
  scans: ScanRecord[];
  repos: PublishedRepo[];
}

/** What GET /api/dashboard returns. Plan and totals are the same shapes the
 *  profile renders — one server-side source, so the two pages can never drift
 *  into disagreeing about the same numbers. */
export interface Dashboard {
  user: User;
  member_since: number | null;
  plan: Profile["plan"];
  totals: Profile["totals"];
  activity: ActivityItem[];
  series: ActivityDay[];
  recent: {
    generations: GenerationRecord[];
    scans: ScanRecord[];
    repos: PublishedRepo[];
  };
}

export interface AppState {
  step: "input" | "analyzing" | "preview" | "configure" | "generating" | "done";
  jobId: string | null;
  analysis: ProjectAnalysis | null;
  result: GenerateResponse | null;
  error: string | null;
  currentProvider: string;
  streamOutput: string;
}
