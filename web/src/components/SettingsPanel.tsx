/*
  Settings: one summary line until you open it.

  "manifest mode · price left blank · descriptions as written" tells an operator
  what will happen without asking them to read seven controls. Everything here
  has a sensible default; provenance, which does not, lives on the main screen
  instead.
*/

import { useState } from "react";
import type { Config, JobSettings } from "../api/client";

interface Props {
  settings: JobSettings;
  onChange: (next: JobSettings) => void;
  config: Config | undefined;
}

const IMAGE_MODE_HELP: Record<string, string> = {
  manifest: "photos saved as files, ready to upload — no image host is contacted",
  url_columns: "photos re-hosted and linked — needs an image host configured",
  both: "files kept and photos re-hosted",
};

export function SettingsPanel({ settings, onChange, config }: Props) {
  const [open, setOpen] = useState(false);
  const set = <K extends keyof JobSettings>(key: K, value: JobSettings[K]) =>
    onChange({ ...settings, [key]: value });

  const summary = [
    `${settings.image_mode.replace("_", " ")} mode`,
    config?.defaults.price_strategy === "blank" ? "price left blank" : "price converted",
    settings.description_mode === "raw" ? "descriptions as written" : "descriptions rewritten",
    settings.llm ? "model assist on" : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const hostsReady = config?.image_hosts.some((h) => h.configured) ?? false;
  const needsHost = settings.image_mode !== "manifest";

  return (
    <section className="settings">
      <button
        className="settings-toggle"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        type="button"
      >
        <span className="summary">{summary}</span>
        <span className="change">{open ? "Hide" : "Change"}</span>
      </button>

      {open && (
        <div className="settings-body">
          <Field label="Photos">
            <select
              value={settings.image_mode}
              onChange={(e) => set("image_mode", e.target.value)}
            >
              {(config?.enums.image_mode ?? ["manifest"]).map((mode) => (
                <option key={mode} value={mode}>
                  {mode.replace("_", " ")}
                </option>
              ))}
            </select>
            <p className="help">{IMAGE_MODE_HELP[settings.image_mode]}</p>
            {needsHost && !hostsReady && (
              <p className="help is-review">
                No image host is configured, so any photo whose own link fails will end with
                no URL. Set one in <code>.env</code>, or stay on manifest.
              </p>
            )}
          </Field>

          <Field label="Descriptions">
            <select
              value={settings.description_mode}
              onChange={(e) => set("description_mode", e.target.value)}
            >
              <option value="raw">as written on the page</option>
              <option value="rewrite">rewritten in your own words</option>
            </select>
            {settings.description_mode === "rewrite" && !config?.llm.configured && (
              <p className="help is-review">
                Rewriting needs a model, and none is configured. Rows will keep the source
                text and say so.
              </p>
            )}
          </Field>

          <Field label="Model assist">
            <label className="check">
              <input
                type="checkbox"
                checked={settings.llm}
                disabled={!config?.llm.configured}
                onChange={(e) => set("llm", e.target.checked)}
              />
              use {config?.llm.model ?? "a language model"}
            </label>
            <p className="help">
              Rewrites descriptions and picks a category from your taxonomy. Never used for
              price, weight, dimensions, HS code or GI region —{" "}
              <span className="depth-low">there is no field for it to put one in.</span>
            </p>
          </Field>

          <Field label="Browser fallback">
            <label className="check">
              <input
                type="checkbox"
                checked={settings.render ?? config?.defaults.render_enabled ?? true}
                onChange={(e) => set("render", e.target.checked)}
              />
              render pages that arrive empty
            </label>
            <p className="help">
              Only for pages with no title, description or photos in the HTML. Roughly three
              seconds each, so it stays a fallback.
            </p>
          </Field>

          <Field label="At once">
            <input
              type="number"
              min={1}
              max={20}
              value={settings.concurrency}
              onChange={(e) => set("concurrency", Number(e.target.value) || 1)}
            />
            <p className="help">
              Across the whole job, never per shop — one host is asked one thing at a time,{" "}
              {config?.defaults.per_domain_delay_s ?? 2}s apart.
            </p>
          </Field>

          <Field label="Note on every row">
            <input
              type="text"
              value={settings.seller_note ?? ""}
              placeholder="optional"
              onChange={(e) => set("seller_note", e.target.value || null)}
            />
          </Field>

          <Field label="robots.txt">
            <label className="check">
              <input
                type="checkbox"
                checked={settings.ignore_robots}
                onChange={(e) => set("ignore_robots", e.target.checked)}
              />
              ignore it
            </label>
            <p className="help is-review">
              Only for sites you own. Honouring robots.txt is the default and should stay
              that way for anyone else's shop.
            </p>
          </Field>
        </div>
      )}
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      <div className="field-body">{children}</div>
    </div>
  );
}
