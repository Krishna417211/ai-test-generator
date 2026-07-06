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

export interface AppState {
  step: "input" | "analyzing" | "preview" | "configure" | "generating" | "done";
  jobId: string | null;
  analysis: ProjectAnalysis | null;
  result: GenerateResponse | null;
  error: string | null;
  currentProvider: string;
  streamOutput: string;
}
