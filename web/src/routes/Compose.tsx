/*
  Compose: paste links, say who made them, press one button.

  Everything else on this screen is progressive disclosure. Settings are one
  summary line until you open them, because burying the primary path under
  configuration is how an operator tool becomes a form.

  The one setting that is NOT collapsed is provenance, and it has no default.
  An unexplained required field feels like bureaucracy; the sentence under it is
  what makes it feel like care instead.
*/

import { useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError, type Config, type JobSettings, type Preflight } from "../api/client";
import { countLines, humanRange } from "../lib/urls";
import { useConfig } from "../hooks/useConfig";
import { PreflightPanel } from "../components/PreflightPanel";
import { SettingsPanel } from "../components/SettingsPanel";

const EXAMPLE = "https://yourshop.com/products/indigo-cotton-stole";

function defaultSettings(config: Config | undefined): JobSettings {
  return {
    provenance: "", // no default, on purpose
    image_mode: config?.defaults.image_mode ?? "manifest",
    description_mode: config?.defaults.description_mode ?? "raw",
    concurrency: config?.defaults.concurrency ?? 5,
    seller_note: null,
    render: null,
    llm: false,
    ignore_robots: false,
  };
}

export function Compose() {
  const navigate = useNavigate();
  const { data: config } = useConfig();

  const [text, setText] = useState("");
  const [settings, setSettings] = useState<JobSettings>(() => defaultSettings(config));
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const provenanceRef = useRef<HTMLFieldSetElement>(null);

  const { lines, counts } = useMemo(() => countLines(text), [text]);
  const flagged = useMemo(
    () => new Map(lines.filter((l) => l.status !== "ok" && l.status !== "blank").map((l) => [l.line, l])),
    [lines],
  );

  const ready = counts.unique > 0 && settings.provenance !== "";

  async function onPreflight() {
    if (!settings.provenance) {
      provenanceRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
      (provenanceRef.current?.querySelector("input") as HTMLInputElement | null)?.focus();
      setError("Say who made this content before starting.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setPreflight(await api.preflight(text.split("\n"), settings));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onStart() {
    setBusy(true);
    setError(null);
    try {
      const created = await api.createJob(text.split("\n"), settings);
      navigate(`/jobs/${created.job_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setBusy(false);
    }
  }

  function onDrop(event: React.DragEvent<HTMLTextAreaElement>) {
    const file = event.dataTransfer.files[0];
    if (!file) return;
    event.preventDefault();
    file.text().then((content) => setText((prev) => (prev ? prev + "\n" : "") + content.trim()));
  }

  return (
    <div className="bench compose">
      <h1 className="screen-title">Product links</h1>
      <p className="lede">
        One per line. Drop a <code>.txt</code> or <code>.csv</code>, or paste a column
        straight out of a spreadsheet.
      </p>

      <div className="compose-grid">
        <div className="compose-input">
          <textarea
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              setPreflight(null);
            }}
            onDrop={onDrop}
            onDragOver={(e) => e.preventDefault()}
            spellCheck={false}
            placeholder={EXAMPLE}
            aria-label="Product links, one per line"
            rows={14}
          />
          {flagged.size > 0 && <FlaggedLines flagged={[...flagged.values()]} />}
          {text.trim() === "" && <EmptyInvitation onUse={() => setText(EXAMPLE)} />}
        </div>

        <Counter counts={counts} />
      </div>

      <fieldset className="provenance" ref={provenanceRef}>
        <legend>
          Who made this content? <span className="required">required</span>
        </legend>
        <div className="choices">
          {[
            ["own", "I made or own it"],
            ["authorised", "I have permission"],
            ["third-party", "Neither"],
          ].map(([value, label]) => (
            <label key={value} className={settings.provenance === value ? "chosen" : ""}>
              <input
                type="radio"
                name="provenance"
                value={value}
                checked={settings.provenance === value}
                onChange={() => {
                  setSettings({ ...settings, provenance: value });
                  setError(null);
                }}
              />
              {label}
            </label>
          ))}
        </div>
        <p className="why">
          Photos and product copy belong to whoever made them, and haat's seller rules turn on
          this. It is a fact only you know, so this tool will not guess it.
        </p>
        {settings.provenance === "third-party" && (
          <p className="warn-note">
            Every row will be marked <strong>needs a human</strong>, photos will not be
            re-hosted, and descriptions will be rewritten rather than copied.
          </p>
        )}
      </fieldset>

      <SettingsPanel settings={settings} onChange={setSettings} config={config} />

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {preflight ? (
        <PreflightPanel
          preflight={preflight}
          busy={busy}
          onBack={() => setPreflight(null)}
          onStart={onStart}
          estimate={humanRange(preflight.estimate_low_s, preflight.estimate_high_s)}
        />
      ) : (
        <div className="actions">
          <button className="primary" onClick={onPreflight} disabled={!ready || busy}>
            {busy ? "Checking" : "Start processing"}
          </button>
          {counts.unique === 0 && <span className="hint">Paste some links to begin.</span>}
          {counts.unique > 0 && !settings.provenance && (
            <span className="hint">Choose who made this content.</span>
          )}
        </div>
      )}
    </div>
  );
}

/** The empty state does one job: show what a product link looks like, and let
 *  someone try the whole flow with one click. A blank box with placeholder text
 *  is not an invitation, it is a wall. */
function EmptyInvitation({ onUse }: { onUse: () => void }) {
  return (
    <p className="invitation">
      A product link looks like <code className="mono">{EXAMPLE}</code>.{" "}
      <button className="linkish" onClick={onUse} type="button">
        Use that one
      </button>{" "}
      to see the whole thing run, or drop a file of your own.
    </p>
  );
}

function Counter({ counts }: { counts: ReturnType<typeof countLines>["counts"] }) {
  const rows: [string, number, string][] = [
    ["pasted", counts.pasted, ""],
    ["unique", counts.unique, "depth-high"],
    ["duplicate", counts.duplicates, counts.duplicates ? "depth-low" : "depth-none"],
    ["not a link", counts.invalid, counts.invalid ? "is-failed" : "depth-none"],
  ];
  return (
    <dl className="counter" aria-live="polite">
      {rows.map(([label, value, tone]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd className={`mono ${tone}`}>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function FlaggedLines({ flagged }: { flagged: { line: number; raw: string; status: string }[] }) {
  // Marked in place, with the line number, so they can be fixed without
  // leaving the screen. A count alone would send someone hunting.
  return (
    <ul className="flagged">
      {flagged.slice(0, 6).map((line) => (
        <li key={line.line}>
          <span className="mono lineno">{line.line}</span>
          <span className={line.status === "invalid" ? "is-failed" : "depth-low"}>
            {line.status === "invalid" ? "not a link" : "same product as an earlier line"}
          </span>
          <span className="mono raw">{line.raw.slice(0, 64)}</span>
        </li>
      ))}
      {flagged.length > 6 && <li className="more">and {flagged.length - 6} more</li>}
    </ul>
  );
}
