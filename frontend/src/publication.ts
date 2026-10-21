import type { EventListing } from "./model";

export const INDEX_URL = "/generated/index.json";
export const CATALOG_REFRESH_MS = 60_000;

export type RegionListing = {
  season: number;
  region: string;
  region_name: string;
  catalog_url: string;
  event_codes: string[];
};
export type PublicationIndex = { regions: RegionListing[] };

export function publicationUrl(path: string, base = import.meta.env?.VITE_PUBLICATION_BASE_URL): string {
  return base ? new URL(path, base).href : path;
}

export function catalogForRoute(path: string, index: PublicationIndex): RegionListing | null {
  const clean = path.replace(/\/$/, "") || "/";
  if (clean === "/") {
    const regions = [...index.regions].sort((a, b) => b.season - a.season || a.region.localeCompare(b.region));
    const latest = regions.filter((region) => region.season === regions[0]?.season);
    return latest.find((region) => region.region === "USPA") ?? latest[0] ?? null;
  }
  return index.regions.find((region) =>
    clean === `/seasons/${region.season}/${region.region}` ||
    region.event_codes.some((code) => clean === `/events/${region.season}/${code}`),
  ) ?? null;
}

export async function readPublication<T>(
  url: string,
  signal?: AbortSignal,
  fetcher: typeof fetch = fetch,
): Promise<T> {
  const response = await fetcher(publicationUrl(url), {
    cache: url === INDEX_URL || url.startsWith("/generated/catalogs/") ? "no-cache" : "default",
    credentials: "omit",
    signal,
  });
  if (!response.ok || !response.headers.get("content-type")?.includes("application/json"))
    throw new Error("Published data is unavailable. Please try again later.");
  return response.json();
}

export function forecastChanged(current: EventListing, next: EventListing | null): boolean {
  return next === null || current.forecast_url !== next.forecast_url;
}
