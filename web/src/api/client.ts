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
  } catch {
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

export interface Preflight {
  pasted: number;
  unique: number;
  duplicates: number;
  invalid: InvalidUrl[];
  domains: Record<string, number>;
  robots_disallowed: string[];
  robots_checked: boolean;
  estimate_low_s: number;
  estimate_high_s: number;
  summary: string;
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

export const api = {
  health: () => request<Health>("/api/health"),
  config: () => request<Config>("/api/config"),

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
