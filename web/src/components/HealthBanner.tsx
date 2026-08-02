import { useHealth } from "../hooks/useConfig";
import { ApiError } from "../api/client";

/*
  Shown only when something is wrong. A permanent green "all systems normal"
  strip is furniture: it trains an operator to stop reading the one place that
  would tell them their taxonomy is incomplete.
*/
export function HealthBanner() {
  const { data, error } = useHealth();

  if (error) {
    const dead = error instanceof ApiError && error.status === 0;
    return (
      <div className="banner banner-fail" role="alert">
        <strong>{dead ? "The agent isn't running." : "The agent isn't answering."}</strong>{" "}
        {dead ? (
          <>
            Start it with <code>haat-lister serve</code> and reload.
          </>
        ) : (
          error.message
        )}
      </div>
    );
  }

  if (!data || data.ok) return null;

  return (
    <div className="banner banner-fail" role="alert">
      <strong>
        {data.blocking} thing{data.blocking === 1 ? "" : "s"} to fix before a job will run.
      </strong>{" "}
      {data.findings
        .filter((f) => f.level === "fail")
        .map((f) => f.title)
        .join(" · ")}
    </div>
  );
}
