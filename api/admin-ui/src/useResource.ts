import { useEffect, useState } from "react";

import { type Resource, getJson } from "./api";

/**
 * Load one endpoint when a section opens, and again when asked.
 *
 * `null` means the request is still in flight, which the sections render as
 * "loading" rather than as an empty result: an empty table that later fills in
 * reads, for the moment it is on screen, as an answer.
 */
export function useResource<T>(path: string, reloadKey = 0): Resource<T> | null {
  const [resource, setResource] = useState<Resource<T> | null>(null);

  useEffect(() => {
    let live = true;
    setResource(null);
    // An empty path means "nothing to ask yet" -- a search box below its
    // minimum length, say. Fetching "" would request the current document and
    // hand the caller an HTML page to parse as JSON.
    if (!path) return;
    getJson<T>(path).then((answer) => {
      if (live) setResource(answer);
    });
    return () => {
      live = false;
    };
  }, [path, reloadKey]);

  return resource;
}
