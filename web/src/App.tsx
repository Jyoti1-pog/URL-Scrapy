/*
  The shell. Navigation is one line of text links -- no icon sidebar.

  That is the difference between this and the admin panel it would otherwise
  become. A rail appears on the job screen (Phase 5) and it carries the fill
  grid: the artefact being made, not a menu. Where there is nothing being made,
  as here, there is no rail, because an empty rail is furniture pretending to be
  information.
*/

import { NavLink, Route, Routes } from "react-router-dom";
import { Compose } from "./routes/Compose";
import { History } from "./routes/History";
import { JobScreen } from "./routes/JobScreen";
import { Review } from "./routes/Review";
import { HealthBanner } from "./components/HealthBanner";
import "./styles/app.css";

export function App() {
  return (
    <>
      {/* First tab stop on every screen. Without it, reaching the review table
          by keyboard means tabbing through the whole header on every page. */}
      <a className="skip" href="#bench">
        Skip to the work
      </a>

      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" className="wordmark">
            haat<span>-lister</span>
          </NavLink>
          <nav>
            <NavLink to="/" end>
              Compose
            </NavLink>
            <NavLink to="/jobs">Jobs</NavLink>
          </nav>
        </div>
      </header>

      <HealthBanner />

      <main id="bench" tabIndex={-1}>
        <Routes>
          <Route path="/" element={<Compose />} />
          <Route path="/jobs" element={<History />} />
          {/* The job id belongs in the URL: a refresh, a bookmark and a second
              tab all have to work, and all three are just this route again. */}
          <Route path="/jobs/:jobId" element={<JobScreen />} />
          <Route path="/jobs/:jobId/review" element={<Review />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </>
  );
}

function NotFound() {
  return (
    <div className="bench">
      <h1 className="screen-title">No such page</h1>
      <p className="lede">
        Nothing lives at this address. <NavLink to="/">Start a new job</NavLink> or{" "}
        <NavLink to="/jobs">look at an old one</NavLink>.
      </p>
    </div>
  );
}
