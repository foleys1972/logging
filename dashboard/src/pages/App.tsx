import { useQuery, useQueryClient } from "@tanstack/react-query";
import axios from "axios";
import { useMemo, useState } from "react";

type SiteStatus = {
  site_id: number;
  name: string;
  region?: string;
  status: "GREEN" | "AMBER" | "RED";
  summary?: string | null;
  last_snapshot_at?: string | null;
};

type Aggregate = {
  label: string;
  value: number;
  descriptor: string;
  tone: "green" | "amber" | "red" | "slate";
};

async function fetchSiteStatuses(): Promise<SiteStatus[]> {
  const response = await axios.get<SiteStatus[]>("/api/v1/sites/status");
  return response.data;
}

async function fetchSiteDetail(siteId: number) {
  const response = await axios.get(`/api/v1/sites/${siteId}/detail`);
  return response.data;
}

function formatTimestamp(timestamp?: string | null): string {
  if (!timestamp) return "Pending baseline";
  return new Date(timestamp).toLocaleString();
}

export default function App() {
  const queryClient = useQueryClient();
  const [selectedSiteId, setSelectedSiteId] = useState<number | null>(null);

  const {
    data: sites = [],
    isLoading,
    isError,
    refetch,
  } = useQuery({ queryKey: ["site-statuses"], queryFn: fetchSiteStatuses, refetchInterval: 60_000 });

  const { data: selectedSite, isFetching: isDetailLoading } = useQuery({
    queryKey: ["site-detail", selectedSiteId],
    queryFn: async () => {
      if (selectedSiteId == null) return null;
      return fetchSiteDetail(selectedSiteId);
    },
    enabled: selectedSiteId != null,
    staleTime: 30_000,
  });

  const aggregates = useMemo<Aggregate[]>(() => {
    const total = sites.length;
    const greens = sites.filter((site) => site.status === "GREEN").length;
    const ambers = sites.filter((site) => site.status === "AMBER").length;
    const reds = sites.filter((site) => site.status === "RED").length;

    return [
      { label: "Total Sites", value: total, descriptor: "monitored", tone: "slate" },
      { label: "Healthy", value: greens, descriptor: "GREEN", tone: "green" },
      { label: "Watch", value: ambers, descriptor: "AMBER", tone: "amber" },
      { label: "Action", value: reds, descriptor: "RED", tone: "red" },
    ];
  }, [sites]);

  if (isLoading) {
    return (
      <div className="app-container loading">
        <div className="hero">
          <div className="hero-text">
            <h1>Global WBA Monitoring</h1>
            <p>Real-time observability with RAG health scoring.</p>
          </div>
        </div>
        <div className="grid skeleton">
          {Array.from({ length: 6 }).map((_, index) => (
            <div key={index} className="tile skeleton-tile" />
          ))}
        </div>
      </div>
    );
  }

  if (isError) {
    return (
      <div className="app-container error">
        <div className="error-card">
          <h2>Unable to load site status</h2>
          <p>Check the control plane service and try again.</p>
          <button type="button" onClick={() => refetch()}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="app-wrapper">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" />
          <div>
            <h1>TradeSense Atlas</h1>
            <p>Global WBA Monitoring</p>
          </div>
        </div>
        <div className="legend">
          <h2>Status Legend</h2>
          <ul>
            <li><span className="dot dot-green" /> Normal operations</li>
            <li><span className="dot dot-amber" /> Change detected / watch</li>
            <li><span className="dot dot-red" /> Immediate action required</li>
          </ul>
        </div>
        <div className="footer-note">Last refresh {new Date().toLocaleTimeString()}</div>
      </aside>

      <main className="main">
        <header className="hero">
          <div className="hero-text">
            <h2>Global Command Center</h2>
            <p>15-site footprint with automated baseline variance detection.</p>
          </div>
          <div className="hero-actions">
            <button type="button" onClick={() => refetch()}>
              Refresh
            </button>
          </div>
        </header>

        <section className="aggregates">
          {aggregates.map((item) => (
            <div key={item.label} className={`aggregate-card tone-${item.tone}`}>
              <h3>{item.label}</h3>
              <strong>{item.value}</strong>
              <span>{item.descriptor}</span>
            </div>
          ))}
        </section>

        <section className="grid">
          {sites.map((site) => (
            <article
              key={site.site_id}
              className={`tile rag-${site.status.toLowerCase()} ${selectedSiteId === site.site_id ? "tile-active" : ""}`}
              onClick={() => {
                setSelectedSiteId(site.site_id);
                queryClient.prefetchQuery({ queryKey: ["site-detail", site.site_id], queryFn: () => fetchSiteDetail(site.site_id) });
              }}
            >
              <header>
                <div>
                  <h3>{site.name}</h3>
                  {site.region && <span className="region">{site.region}</span>}
                </div>
                <span className={`rag-pill pill-${site.status.toLowerCase()}`}>{site.status}</span>
              </header>
              <p className="summary">{site.summary ?? "Baseline pending"}</p>
              <footer>
                <span className="timestamp">Updated {formatTimestamp(site.last_snapshot_at)}</span>
              </footer>
            </article>
          ))}

          {sites.length === 0 && (
            <div className="empty">
              <h3>No sites onboarded</h3>
              <p>Add sites via the control plane to begin monitoring.</p>
            </div>
          )}
        </section>
      </main>

      {selectedSite && (
        <aside className={`detail-panel ${isDetailLoading ? "loading" : ""}`}>
          <header>
            <div>
              <h3>{selectedSite.name}</h3>
              {selectedSite.region && <span className="region">{selectedSite.region}</span>}
            </div>
            <button type="button" onClick={() => setSelectedSiteId(null)}>
              Close
            </button>
          </header>
          <section className="detail-summary">
            <span className={`rag-pill pill-${selectedSite.overall_status.toLowerCase()}`}>{selectedSite.overall_status}</span>
            <p>Last snapshot: {formatTimestamp(selectedSite.last_snapshot_at)}</p>
          </section>
          <section className="detail-components">
            <h4>Component Health</h4>
            <ul>
              {selectedSite.components.map((component: any) => (
                <li key={component.component}>
                  <div className="component-header">
                    <span className="component-name">{component.component}</span>
                    <span className={`rag-pill pill-${component.status.toLowerCase()}`}>{component.status}</span>
                  </div>
                  <p>{component.summary ?? "No summary available"}</p>
                  <span className="timestamp">Updated {formatTimestamp(component.last_updated)}</span>
                  {component.reasons?.length > 0 && (
                    <details>
                      <summary>Details</summary>
                      <ul>
                        {component.reasons.map((reason: any, idx: number) => (
                          <li key={idx}>
                            <strong>{reason.code}</strong>: {reason.message}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </li>
              ))}
            </ul>
          </section>
        </aside>
      )}
    </div>
  );
}


