import { Box, Plug } from "lucide-react";

import googleMark from "./assets/google.png";
import slackMark from "./assets/slack.png";

/**
 * Brand marks, inlined.
 *
 * Inline SVG rather than image files, because the panel's Content-Security-
 * Policy allows images only from this origin and a brand CDN is exactly the
 * kind of third party it exists to exclude. Inlining also means no extra
 * requests and no dependency for four paths.
 *
 * The path data is from Simple Icons, which publishes it under CC0. The marks
 * themselves remain the trademarks of their owners and are used here only to
 * identify the service or vendor they belong to.
 *
 * Slack and Google are the exceptions: their marks are the official
 * multi-colour assets, bundled as images. Both have transparent backgrounds
 * and read correctly on either theme, so unlike the wordmark they need no
 * inversion. Simple Icons carries only monochrome glyphs, which is why they
 * are not taken from there.
 *
 * GitHub stays an inline path in `currentColor`, and that is not an
 * inconsistency: its mark is monochrome by design, published as black or
 * white, so following the text colour is the correct treatment and gives the
 * right one on both themes without a second file.
 *
 * One gap remains, and it is deliberate rather than an oversight:
 *
 * - **Most models have no mark.** A model id is an arbitrary string chosen by
 *   whoever runs the serving layer, so the vendor is inferred from it by
 *   substring. That is a good guess for `llama-…` and `mistral-…` and no guess
 *   at all for `acme-internal-7b`, which is the common case on a private hub.
 *   Unrecognised models get the neutral glyph, which is honest: we do not know
 *   who made it.
 */

function Mark({ path, label, size = 18 }: { path: string; label: string; size?: number }) {
  return (
    <svg
      className="brand-mark"
      role="img"
      aria-label={label}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="currentColor"
    >
      <path d={path} />
    </svg>
  );
}

const GITHUB = "M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12";
const META = "M6.915 4.03c-1.968 0-3.683 1.28-4.871 3.113C.704 9.208 0 11.883 0 14.449c0 .706.07 1.369.21 1.973a6.624 6.624 0 0 0 .265.86 5.297 5.297 0 0 0 .371.761c.696 1.159 1.818 1.927 3.593 1.927 1.497 0 2.633-.671 3.965-2.444.76-1.012 1.144-1.626 2.663-4.32l.756-1.339.186-.325c.061.1.121.196.183.3l2.152 3.595c.724 1.21 1.665 2.556 2.47 3.314 1.046.987 1.992 1.22 3.06 1.22 1.075 0 1.876-.355 2.455-.843a3.743 3.743 0 0 0 .81-.973c.542-.939.861-2.127.861-3.745 0-2.72-.681-5.357-2.084-7.45-1.282-1.912-2.957-2.93-4.716-2.93-1.047 0-2.088.467-3.053 1.308-.652.57-1.257 1.29-1.82 2.05-.69-.875-1.335-1.547-1.958-2.056-1.182-.966-2.315-1.303-3.454-1.303zm10.16 2.053c1.147 0 2.188.758 2.992 1.999 1.132 1.748 1.647 4.195 1.647 6.4 0 1.548-.368 2.9-1.839 2.9-.58 0-1.027-.23-1.664-1.004-.496-.601-1.343-1.878-2.832-4.358l-.617-1.028a44.908 44.908 0 0 0-1.255-1.98c.07-.109.141-.224.211-.327 1.12-1.667 2.118-2.602 3.358-2.602zm-10.201.553c1.265 0 2.058.791 2.675 1.446.307.327.737.871 1.234 1.579l-1.02 1.566c-.757 1.163-1.882 3.017-2.837 4.338-1.191 1.649-1.81 1.817-2.486 1.817-.524 0-1.038-.237-1.383-.794-.263-.426-.464-1.13-.464-2.046 0-2.221.63-4.535 1.66-6.088.454-.687.964-1.226 1.533-1.533a2.264 2.264 0 0 1 1.088-.285z";
const MISTRAL = "M17.143 3.429v3.428h-3.429v3.429h-3.428V6.857H6.857V3.43H3.43v13.714H0v3.428h10.286v-3.428H6.857v-3.429h3.429v3.429h3.429v-3.429h3.428v3.429h-3.428v3.428H24v-3.428h-3.43V3.429z";

export function ConnectorIcon({ connector }: { connector: string }) {
  if (connector === "github") return <Mark path={GITHUB} label="GitHub" />;
  if (connector === "google") {
    return <img className="brand-image" src={googleMark} alt="Google Workspace" />;
  }
  if (connector === "slack") {
    return <img className="brand-image" src={slackMark} alt="Slack" />;
  }
  return <Plug size={16} strokeWidth={1.75} aria-hidden="true" />;
}

/**
 * The vendor a model id appears to come from, or nothing.
 *
 * Substring matching, which is a guess and is treated as one: anything
 * unrecognised gets a neutral glyph rather than a wrong logo. Ordering matters
 * only in that these prefixes do not overlap today.
 */
export function ModelIcon({ model }: { model: string }) {
  const id = model.toLowerCase();
  if (id.includes("llama")) return <Mark path={META} label="Meta" />;
  if (id.includes("mistral") || id.includes("mixtral")) return <Mark path={MISTRAL} label="Mistral AI" />;
  return <Box size={16} strokeWidth={1.75} aria-hidden="true" />;
}
