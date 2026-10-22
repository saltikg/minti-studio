(() => {
  const nodes = Array.from(document.querySelectorAll("[data-ingestion-progress][data-video-pk]"));
  if (!nodes.length) return;

  const POLL_MS = 2500;
  let timer = null;
  let requestRunning = false;
  let lastSuccessfulPollAt = Date.now();
  let stoppedByClientError = false;

  const formatEta = (seconds) => {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return "";
    if (value < 60) return "<1 min left";
    const minutes = Math.max(1, Math.round(value / 60));
    return `~${minutes} min left`;
  };

  const ensureContents = (node) => {
    if (node.querySelector("[data-ingestion-message]")) return;
    node.innerHTML = `
      <div class="vs-ingestion-progress-copy" aria-live="polite">
        <span data-ingestion-message></span>
      </div>
      <div class="vs-ingestion-progress-track" data-ingestion-track>
        <div class="vs-ingestion-progress-bar" data-ingestion-bar></div>
      </div>
      <button type="button" class="vs-ingestion-progress-retry" data-ingestion-retry hidden>Retry</button>
    `;
  };

  const updateNode = (node, status) => {
    ensureContents(node);
    const message = node.querySelector("[data-ingestion-message]");
    const bar = node.querySelector("[data-ingestion-bar]");
    const track = node.querySelector("[data-ingestion-track]");
    const retry = node.querySelector("[data-ingestion-retry]");
    const pill = node.closest(".mv-status-cell")?.querySelector("[data-ingestion-pill]");
    const stage = String(status?.stage || "");
    const rawPercent = status?.percent;
    const percentValue = Number(rawPercent);
    const hasPercent = rawPercent !== null && rawPercent !== "" && Number.isFinite(percentValue) && percentValue >= 0;
    const wasActive = node.dataset.wasActive === "1";
    const isActive = Boolean(status?.active);
    const failed = Boolean(status?.failed);
    const eta = formatEta(status?.eta_seconds);
    let copy = String(status?.message || (isActive ? "Processing…" : "Ready"));
    if (hasPercent && stage === "downloading") copy += ` ${Math.round(percentValue)}%`;
    if (eta && stage === "downloading") copy += ` · ${eta}`;
    if (failed && status?.error) copy = String(status.error);
    message.textContent = copy;
    node.hidden = !isActive && !failed && stage === "ready";
    node.classList.toggle("is-failed", failed);
    node.classList.toggle("is-indeterminate", isActive && !hasPercent);
    track.hidden = !isActive;
    bar.style.width = hasPercent ? `${Math.max(0, Math.min(100, percentValue))}%` : "0%";
    retry.hidden = !failed;
    node.dataset.wasActive = isActive || wasActive ? "1" : "0";
    if (pill) {
      pill.textContent = failed ? "Failed" : (isActive ? "Processing" : "Ready");
      pill.className = `mv-status-pill mv-status-pill--${failed ? "failed" : (isActive ? "processing" : "ready")}`;
    }
    if (wasActive && stage === "ready" && node.dataset.reloadOnReady === "1") {
      window.location.reload();
    }
  };

  const showConnectionState = () => {
    if ((Date.now() - lastSuccessfulPollAt) < 60000) return;
    nodes.filter((node) => node.dataset.wasActive === "1").forEach((node) => {
      ensureContents(node);
      node.hidden = false;
      node.classList.add("is-indeterminate");
      node.querySelector("[data-ingestion-message]").textContent = "Still working…";
    });
  };

  const groups = () => {
    const result = new Map();
    nodes.forEach((node) => {
      const url = String(node.dataset.statusUrl || "");
      if (!url) return;
      if (!result.has(url)) result.set(url, []);
      result.get(url).push(node);
    });
    return result;
  };

  const poll = async () => {
    if (requestRunning || document.hidden || stoppedByClientError) return;
    requestRunning = true;
    let allRequestsSucceeded = true;
    let anyActive = false;
    try {
      for (const [baseUrl, groupNodes] of groups()) {
        const ids = groupNodes.map((node) => node.dataset.videoPk).filter(Boolean);
        const url = new URL(baseUrl, window.location.href);
        url.searchParams.set("video_ids", ids.join(","));
        const response = await fetch(url.toString(), {
          credentials: "same-origin",
          cache: "no-store",
          headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        const payload = await response.json().catch(() => ({}));
        if (response.status >= 400 && response.status < 500) {
          stoppedByClientError = true;
          throw new Error(payload.message || "Progress is unavailable.");
        }
        if (!response.ok || !payload.ok) throw new Error(payload.message || "Progress check failed.");
        lastSuccessfulPollAt = Date.now();
        groupNodes.forEach((node) => {
          const status = payload.statuses?.[String(node.dataset.videoPk)];
          if (status) {
            anyActive = anyActive || Boolean(status.active);
            updateNode(node, status);
          }
        });
      }
    } catch (_) {
      allRequestsSucceeded = false;
      showConnectionState();
    } finally {
      requestRunning = false;
      if (allRequestsSucceeded && !anyActive && timer) {
        window.clearInterval(timer);
        timer = null;
      }
    }
  };

  const wakePolling = () => {
    stoppedByClientError = false;
    void poll();
    if (!timer) timer = window.setInterval(poll, POLL_MS);
  };

  nodes.forEach((node) => {
    ensureContents(node);
    const retry = node.querySelector("[data-ingestion-retry]");
    retry?.addEventListener("click", async () => {
      const retryUrl = String(node.dataset.retryUrl || "");
      if (!retryUrl) return;
      retry.disabled = true;
      node.querySelector("[data-ingestion-message]").textContent = "Restarting…";
      try {
        const response = await fetch(retryUrl, {
          method: "POST",
          credentials: "same-origin",
          headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload.ok) throw new Error(payload.message || "Retry failed.");
        stoppedByClientError = false;
        node.dataset.wasActive = "1";
        wakePolling();
      } catch (error) {
        node.querySelector("[data-ingestion-message]").textContent = error.message || "Retry failed.";
      } finally {
        retry.disabled = false;
      }
    });
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) wakePolling();
  });
  window.addEventListener("focus", wakePolling);
  wakePolling();
  window.addEventListener("pagehide", () => window.clearInterval(timer), { once: true });
})();
