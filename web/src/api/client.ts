/*
  The typed client. Shapes mirror haat_lister/api/schemas.py; if they drift, the
  server's 422 is the thing that tells you, not this file.

  The token is read from the URL once and kept in memory. `serve` prints a link
  with `?token=…` when it is bound off loopback, and every request carries it
  from then on -- including EventSource, which cannot set a header and is the
  whole reason the API accepts the query form at all.
*/

const token = new URLSearchParams(location.search).get("token");

export function withToken(path: string): string {
  if (!token) return path;
  return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token);
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(withToken(path), {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch (err) {
    // An aborted request is not a failure -- the live parse cancels the
    // previous keystroke's call on every new one, and reporting those as "the
    // agent isn't running" would make the box flash an error while you type.
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    // A dead backend is a specific, fixable situation, and saying so beats
    // "Failed to fetch".
    throw new ApiError(0, "The agent isn't running.");
  }

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(response.status, detailOf(body) ?? response.statusText);
  }
  return response.json() as Promise<T>;
}

/**
 * The same thing for a file. A sibling of `request` rather than a flag on it:
 * multipart needs the Content-Type header ABSENT so the browser can write the
 * boundary into it, and `request` sets `application/json` unconditionally.
 * Passing a FormData through there produces a body the server cannot parse and
 * an error that says nothing about why.
 */
async function upload<T>(path: string, form: FormData): Promise<T> {
  let response: Response;
  try {
    response = await fetch(withToken(path), { method: "POST", body: form });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "The agent isn't running.");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(response.status, detailOf(body) ?? response.statusText);
  }
  return response.json() as Promise<T>;
}

/** FastAPI returns `detail` as a string, or as a list for a 422. */
function detailOf(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const field = Array.isArray(d?.loc) ? d.loc[d.loc.length - 1] : "";
        return field ? `${field}: ${d.msg}` : d.msg;
      })
      .join("; ");
  }
  return null;
}

// -- shapes -----------------------------------------------------------------

export interface Finding {
  level: "fail" | "warn" | "info";
  title: string;
  detail: string;
  fix: string;
}

export interface Health {
  ok: boolean;
  version: string;
  config_path: string;
  taxonomy_path: string;
  user_agent: string;
  blocking: number;
  warnings: number;
  findings: Finding[];
}

export interface Subcategory {
  slug: string;
  label: string;
  derived: boolean;
}

export interface Category {
  slug: string;
  label: string;
  subcategories: Subcategory[];
}

export interface Config {
  version: string;
  taxonomy_complete: boolean;
  fallback_category: string;
  categories: Category[];
  enums: Record<string, string[]>;
  image_hosts: { name: string; configured: boolean }[];
  llm: { configured: boolean; model: string; used_for: string[]; never_used_for: string[] };
  defaults: {
    image_mode: string;
    price_strategy: string;
    description_mode: string;
    concurrency: number;
    render_enabled: boolean;
    per_domain_delay_s: number;
  };
  locked_fields: string[];
  allow_private_hosts: string[];
}

export interface JobSettings {
  provenance: string;
  image_mode: string;
  description_mode: string;
  concurrency: number;
  seller_note: string | null;
  render: boolean | null;
  llm: boolean;
  ignore_robots: boolean;
}

export interface InvalidUrl {
  line: number;
  raw: string;
  reason: string;
}

export interface ParsedLink {
  line: number;
  original: string;
  canonical: string;
  host: string;
  status: "ok" | "duplicate" | "invalid";
  assumed_scheme: boolean;
  note: string;
}

export interface Parse {
  pasted: number;
  unique: number;
  duplicates: number;
  invalid: number;
  links: ParsedLink[];
  unparsed: InvalidUrl[];
  domains: Record<string, number>;
  truncated: boolean;
  summary: string;
}

export interface Sheet {
  exists: boolean;
  rows: number;
  jobs: number;
  first_added: string;
  last_added: string;
  bytes: number;
  header_ok: boolean;
  folder: string;
  columns: string[];
  preview: string[][];
  preview_limit: number;
  warnings: Finding[];
}

export interface MasterResult {
  added: number;
  replaced: number;
  skipped: number;
  total: number;
  error: string;
}

/* --- Find photos -------------------------------------------------------- */

export interface ParsedTable {
  columns: string[];
  url_column: string;
  url_column_hits: number;
  had_header: boolean;
  delimiter: string;
  found: number;
  preview: string[];
  extras_preview: Record<string, string>[];
  unparsed: string[];
}

export interface FindRow {
  index: number;
  source_url: string;
  title: string;
  title_original: string;
  primary_image_url: string;
  image_urls: string[];
  image_count: number;
  width: number | null;
  height: number | null;
  method: string;
  reason: string;
  explanation: string;
  price: string;
  currency: string;
  category: string;
  description: string;
  weight_g: number | null;
  dimensions: string;
  /** The operator's own CSV columns, carried through. */
  extra: Record<string, string>;
  failed: boolean;
  from_cache: boolean;
}

export interface FindCreated {
  find_id: string;
  accepted: number;
}

export interface FindStart {
  urls?: string[];
  file_text?: string;
  url_column?: string;
  concurrency?: number;
  use_cache?: boolean;
}

/* --- the "Why no photo?" report ---------------------------------------- */

export interface DiagnoseAttempt {
  rung: string;
  transport: string;
  outcome: string;
  elapsed_ms: number;
  ok: boolean;
  detail: string;
}

export interface DiagnoseFetch {
  ok: boolean;
  status_code: number | null;
  content_type: string;
  bytes: number;
  elapsed_ms: number;
  final_url: string;
  redirected: boolean;
  robots_checked: boolean;
  robots_allowed: boolean;
  error_reason: string;
  error_detail: string;
  attempts: DiagnoseAttempt[];
}

/* §3.1. Three states, not two -- "not reached" is not a finding, and typing
   these as booleans is what let the console render it as one. */
export type Check = "yes" | "no" | "not reached";

export interface DiagnoseShape {
  evaluated: boolean;
  looks_like_product: Check;
  captcha: Check;
  login_wall: Check;
  unavailable: Check;
  unavailable_in_buy_box: Check;
  buy_box_found: Check;
  thin: Check;
  verdict: string;
  evidence: string[];
  product_signals: string[];
}

export interface DiagnoseStep {
  predicate: number;
  name: string;
  outcome: string;
  detail: string;
}

export interface DiagnoseCandidate {
  index: number;
  url: string;
  rule: string;
  source: string;
  checked: boolean;
  ok: boolean;
  reason: string;
  stopped_at: number | null;
  width: number | null;
  height: number | null;
  content_type: string;
  content_length: number | null;
  steps: DiagnoseStep[];
}

export interface Diagnosis {
  url: string;
  elapsed_ms: number;
  fetch: DiagnoseFetch;
  shape: DiagnoseShape;
  title: { value: string; source: string; confidence: string; note: string };
  stage_b: {
    enabled: boolean;
    /* §3.3. Decided server-side so the CLI and the console cannot reach
       different words about the same report. */
    state: string;
    triggers: string[];
    attempted: boolean;
    ok: boolean;
    error: string;
    gained: string[];
    candidates_before: number;
    candidates_after: number;
  };
  images: {
    /* False when no page arrived. `kept 0 of 0` reads exactly like an empty
       gallery, and one of those is a shop problem and the other is ours. */
    collected: boolean;
    rules: { rule: string; found: number }[];
    raw_found: number;
    dropped: { url: string; why: string }[];
    candidates: DiagnoseCandidate[];
    plugin_used: string;
    plugin_replaced_candidates: boolean;
    method: string;
    winner: string;
    reason: string;
    explanation: string;
  };
  thresholds: {
    min_width: number;
    min_height: number;
    min_bytes: number;
    max_images_per_product: number;
    hotlink_test: boolean;
  };
  structured_syntaxes: string[];
  shape_enforced: boolean;
}

export interface Preflight {
  pasted: number;
  unique: number;
  duplicates: number;
  invalid: InvalidUrl[];
  domains: Record<string, number>;
  robots_disallowed: string[];
  robots_checked: boolean;
  /* §4.4. What these hosts DID last time, as against what robots.txt allows.
     History, never law -- there is deliberately no field here that could
     prevent the job, and the console must not invent one. */
  observed: ObservedHost[];
  estimate_low_s: number;
  estimate_high_s: number;
  summary: string;
}

export interface ObservedHost {
  host: string;
  urls: number;
  reason: string;
  detail: string;
}

export interface JobCreated {
  job_id: string;
  accepted: number;
  duplicates_removed: number;
  invalid: InvalidUrl[];
  queued_behind: number;
}

export interface JobRow {
  input_index: number;
  source_url: string;
  outcome: string | null;
  row_key: string | null;
  title: string;
  status: string;
  image_tier: string;
  reason: string;
  /** §4.6's closed enum, and the sentence for it. Empty unless the row ended
   *  with no photo. */
  image_problem: string;
  image_explanation: string;
  needs_human: boolean;
  missing: string[];
  /** One character per CSV column: 3 high, 2 medium, 1 low, 0 nothing,
   *  - locked. Empty until the row has produced something. */
  cells: string;
}

export interface Artifact {
  name: string;
  filename: string;
  bytes: number;
  rows: number | null;
}

export interface Job {
  job_id: string;
  state: string;
  created_at: string;
  finished_at: string | null;
  settings: Record<string, unknown>;
  counts: Record<string, number>;
  total: number;
  processed: number;
  written: number;
  failed: number;
  /** The site declined and stopping was correct. Never retried. */
  refused: number;
  needs_human: number;
  running: boolean;
  queued: boolean;
  rows: JobRow[];
  artifacts: Artifact[];
  /** The 19 column names in header order, sent rather than hardcoded here:
   *  the CSV contract lives in one Python module and the grid is a picture
   *  of that, not of a copy this file forgot to update. */
  columns: string[];
  host_calls: number;
  /** What this job did to the sheet. Null when master was off. */
  master: MasterResult | null;
  pages_rendered: number;
  duration_s: number | null;
}

export interface Cell {
  field: string;
  value: string;
  confidence: string;
  source: string;
  editable: boolean;
  edited: boolean;
  /** What the page said, present only when the cell has been edited. The
   *  extraction is stored beside the edit rather than under it. */
  original: string | null;
  note: string | null;
  locked_reason: string | null;
}

export interface RowTable {
  row_key: string;
  input_index: number;
  source_url: string;
  status: string;
  needs_human: boolean;
  missing: string[];
  low_confidence: string[];
  notes: string[];
  cells: Cell[];
}

export interface RowPage {
  job_id: string;
  total: number;
  offset: number;
  limit: number;
  columns: string[];
  editable: string[];
  rows: RowTable[];
  pending_edits: number;
}

export interface ExportResult {
  job_id: string;
  rows: number;
  edits_applied: number;
  rows_edited: number;
}

export interface JobSummary {
  job_id: string;
  state: string;
  created_at: string;
  finished_at: string | null;
  input_count: number;
  counts: Record<string, number>;
}

// -- calls ------------------------------------------------------------------

/* --- §4: import, and §4.4: preflight ------------------------------------ */

export interface ImportColumn {
  index: number;
  header: string;
  samples: string[];
  target: string;
  confidence: number;
  /** "" when unrecognised; otherwise the field it plainly is and we cannot write. */
  known_unused: string;
}

export interface ImportInspect {
  kind: "export" | "saved_page";
  filename: string;
  signature: string;
  profile_used: string;
  columns: ImportColumn[];
  row_count: number;
  /** Sent by the server so the console cannot offer a target it does not have. */
  targets: string[];
  source_url: string;
}

export interface ImportRun {
  rows: {
    source_url: string;
    title: string;
    status: string;
    image_method: string;
    no_image_reason: string;
    notes: string[];
  }[];
  written: number;
  needs_human: number;
  failed: number;
  profile_saved: string;
}

export const api = {
  health: () => request<Health>("/api/health"),
  config: () => request<Config>("/api/config"),

  sheet: () => request<Sheet>("/api/sheet"),

  parseFindFile: (text: string, url_column?: string) =>
    request<ParsedTable>("/api/find/parse-file", {
      method: "POST",
      body: JSON.stringify({ text, url_column: url_column ?? "" }),
    }),

  startFind: (body: FindStart) =>
    request<FindCreated>("/api/find", { method: "POST", body: JSON.stringify(body) }),

  cancelFind: (id: string) => request<unknown>(`/api/find/${id}/cancel`, { method: "POST" }),

  findStream: (id: string) => new EventSource(withToken(`/api/find/${id}/stream`)),

  findDownloadUrl: (id: string) => withToken(`/api/find/${id}/download`),

  diagnose: (url: string) => request<Diagnosis>(`/api/diagnose?url=${encodeURIComponent(url)}`),

  inspectImport: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return upload<ImportInspect>("/api/import/inspect", form);
  },

  runImport: (opts: {
    file: File;
    provenance: string;
    mapping?: Record<string, string>;
    source_url?: string;
    save_profile?: string;
  }) => {
    const form = new FormData();
    form.append("file", opts.file);
    // Required by the server's signature, not by a check it could forget.
    form.append("provenance", opts.provenance);
    if (opts.mapping) form.append("mapping", JSON.stringify(opts.mapping));
    if (opts.source_url) form.append("source_url", opts.source_url);
    if (opts.save_profile) form.append("save_profile", opts.save_profile);
    return upload<ImportRun>("/api/import/run", form);
  },


  parse: (urls: string[], signal?: AbortSignal) =>
    request<Parse>("/api/jobs/parse", {
      method: "POST",
      body: JSON.stringify({ urls }),
      signal,
    }),

  preflight: (urls: string[], settings: JobSettings) =>
    request<Preflight>("/api/jobs/preflight", {
      method: "POST",
      body: JSON.stringify({ urls, settings }),
    }),

  createJob: (urls: string[], settings: JobSettings) =>
    request<JobCreated>("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ urls, settings }),
    }),

  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  jobs: () => request<JobSummary[]>("/api/jobs"),
  cancel: (id: string) => request<unknown>(`/api/jobs/${id}/cancel`, { method: "POST" }),
  resume: (id: string) => request<unknown>(`/api/jobs/${id}/resume`, { method: "POST" }),

  rows: (id: string, opts: { flaggedOnly?: boolean; offset?: number; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.flaggedOnly) q.set("flagged_only", "true");
    if (opts.offset) q.set("offset", String(opts.offset));
    if (opts.limit) q.set("limit", String(opts.limit));
    return request<RowPage>(`/api/jobs/${id}/rows?${q}`);
  },

  editRow: (id: string, rowKey: string, fields: Record<string, string>) =>
    request<RowTable>(`/api/jobs/${id}/rows/${rowKey}`, {
      method: "PATCH",
      body: JSON.stringify({ fields }),
    }),

  editRows: (id: string, rowKeys: string[], fields: Record<string, string>) =>
    request<{ applied: number; rejected: { row_key: string; reason: string }[] }>(
      `/api/jobs/${id}/rows`,
      { method: "PATCH", body: JSON.stringify({ row_keys: rowKeys, fields }) },
    ),

  undoEdit: (id: string, rowKey: string, field: string) =>
    request<RowTable>(`/api/jobs/${id}/rows/${rowKey}/edits/${field}`, { method: "DELETE" }),

  export: (id: string) => request<ExportResult>(`/api/jobs/${id}/export`, { method: "POST" }),
};
