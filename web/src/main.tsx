import "@fontsource/inter/latin-400.css";
import "@fontsource/inter/latin-500.css";
import "@fontsource/inter/latin-600.css";
import "@fontsource/inter/latin-700.css";
import "@fontsource/ibm-plex-mono/latin-400.css";
import "./styles.css";

import { Component, StrictMode } from "react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";

import { captureToken } from "./api";
import { App } from "./App";

/** If anything in the console fails to render, say so plainly instead of leaving a blank page. */
class CrashScreen extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="crash" role="alert">
        <h1>Something went wrong in the console.</h1>
        <p>Nothing was applied to live data because of this. Reload the page to carry on; every rehearsal is kept on
          the server.</p>
        <button type="button" className="choice primary" onClick={() => window.location.reload()}>Reload</button>
      </main>
    );
  }
}

captureToken();

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root element");
createRoot(root).render(
  <StrictMode>
    <CrashScreen>
      <App />
    </CrashScreen>
  </StrictMode>,
);
