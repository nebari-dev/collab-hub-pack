import { LogOut, Moon, Sun } from "lucide-react";
import { useState } from "react";

import logo from "./assets/collab-logo.png";
import { initials } from "./avatar";
import type { AdminSession } from "./session";
import { signOut } from "./signout";
import type { Theme } from "./theme";

/**
 * The product header: the wordmark, what this surface is, and who you are.
 *
 * The logo is the product's own primary horizontal wordmark, bundled rather
 * than linked, so the panel's Content-Security-Policy keeps naming no external
 * origin. It is a navy wordmark and the only variant that ships, so the
 * stylesheet inverts it to white on dark surfaces -- the same treatment the
 * desktop client applies, rather than a second asset to keep in step.
 */
export function Header({
  session,
  theme,
  onToggleTheme,
}: {
  session: AdminSession;
  theme: Theme;
  onToggleTheme: () => void;
}) {
  const [signOutFailed, setSignOutFailed] = useState(false);

  async function leave() {
    const next = await signOut(session.csrf_token);
    if (next) window.location.href = next;
    else setSignOutFailed(true);
  }

  const nextLabel = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";

  return (
    <header className="header">
      <div className="header-brand">
        <img className="wordmark" src={logo} alt="OpenTeams Collab" />
        <span className="header-surface">Admin</span>
      </div>
      <div className="header-identity">
        <span className="avatar" aria-hidden="true">
          {initials(session)}
        </span>
        {signOutFailed && <span className="bad">Sign-out did not go through. Try again.</span>}
        <span className="who">{session.email || session.user}</span>
        <button
          type="button"
          className="icon-button"
          onClick={onToggleTheme}
          title={nextLabel}
          aria-label={nextLabel}
        >
          {theme === "dark" ? (
            <Sun size={16} strokeWidth={1.75} aria-hidden="true" />
          ) : (
            <Moon size={16} strokeWidth={1.75} aria-hidden="true" />
          )}
        </button>
        <button type="button" className="icon-button" onClick={leave} title="Sign out" aria-label="Sign out">
          <LogOut size={16} strokeWidth={1.75} aria-hidden="true" />
        </button>
      </div>
    </header>
  );
}
