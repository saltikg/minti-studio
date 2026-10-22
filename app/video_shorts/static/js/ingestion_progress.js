(() => {
  const POLL_MS = 2500;
  const nodeSelector = "[data-ingestion-progress][data-video-pk], [data-ingestion-card]";
  let timer = null;
  let requestRunning = false;
  let autoOpenClaimed = false;
  let autoOpenSequence = 0;
  const nodes = () => Array.from(document.querySelectorAll(nodeSelector));

  const formatEta = (seconds) => {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return "";
    return value < 60 ? "<1 min left" : `~${Math.max(1, Math.round(value / 60))} min left`;
  };

  const formatRenderEta = (seconds) => {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value <= 0) return "";
    if (value < 45) return "<1 min";
    return `~${Math.max(1, Math.round(value / 60))} min`;
  };

  const cardMeta = (node) => {
    const title = node.querySelector("[data-ingestion-card-title]");
    const meta = node.querySelector("[data-ingestion-card-meta]");
    const image = node.querySelector("[data-ingestion-card-image]");
    if (title) {
      title.textContent = node.dataset.videoTitle || "Video";
      title.title = node.dataset.videoTitle || "Video";
    }
    if (meta) meta.textContent = [node.dataset.videoDuration, node.dataset.videoChannel].filter(Boolean).join(" · ");
    if (image && node.dataset.videoThumbnail) {
      image.src = node.dataset.videoThumbnail;
      image.hidden = false;
    }
  };

  const ensureContents = (node) => {
    if (node.dataset.ingestionReady === "1") return;
    if (node.hasAttribute("data-ingestion-card")) {
      node.innerHTML = `
        <div class="vs-ingestion-card-head">
          <div class="vs-ingestion-card-thumb"><img data-ingestion-card-image alt="" hidden><i class="bi bi-film" aria-hidden="true"></i></div>
          <div class="vs-ingestion-card-copy"><div class="vs-ingestion-card-title" data-ingestion-card-title></div><div class="vs-ingestion-card-meta" data-ingestion-card-meta></div></div>
          <div class="vs-ingestion-card-ready" data-ingestion-ready hidden><span data-ingestion-ready-text>Ready ✓</span><a class="vs-ingestion-open" data-ingestion-open>Open</a></div>
          <div class="vs-ingestion-status-ring is-indeterminate" data-ingestion-ring role="status" aria-live="polite" aria-atomic="true">
            <svg viewBox="0 0 48 48" aria-hidden="true"><circle class="vs-ingestion-ring-track" cx="24" cy="24" r="19" pathLength="100"></circle><circle class="vs-ingestion-ring-value" data-ingestion-ring-value cx="24" cy="24" r="19" pathLength="100"></circle></svg>
            <span class="vs-ingestion-ring-center" aria-hidden="true"><span data-ingestion-ring-percent></span><i class="bi bi-gear" data-ingestion-ring-icon></i></span>
            <span class="visually-hidden" data-ingestion-ring-label>Processing</span>
          </div>
        </div>
        <div class="vs-ingestion-steps" data-ingestion-steps>
          <div class="vs-ingestion-step" data-ingestion-step="source"><span class="vs-ingestion-step-dot"></span><span data-ingestion-source-label>Download</span></div>
          <div class="vs-ingestion-step" data-ingestion-step="transcribing"><span class="vs-ingestion-step-dot"></span><span>Transcribe</span></div>
          <div class="vs-ingestion-step" data-ingestion-step="finding_clips"><span class="vs-ingestion-step-dot"></span><span>Find clips</span></div>
          <div class="vs-ingestion-step" data-ingestion-step="creating_clip"><span class="vs-ingestion-step-dot"></span><span>First clip</span></div>
        </div>
        <div class="vs-ingestion-active-row" data-ingestion-active-row>
          <div class="vs-ingestion-progress-track" data-ingestion-track><div class="vs-ingestion-progress-bar" data-ingestion-bar></div></div>
          <div class="vs-ingestion-progress-copy" aria-live="polite"><span data-ingestion-message></span></div>
        </div>
        <button type="button" class="vs-ingestion-progress-retry" data-ingestion-retry hidden>Retry</button>`;
      const sourceLabel = node.querySelector("[data-ingestion-source-label]");
      if (sourceLabel) sourceLabel.textContent = node.dataset.sourceKind === "upload" ? "Upload" : "Download";
      const open = node.querySelector("[data-ingestion-open]");
      if (open) open.href = node.dataset.openUrl || "#";
      cardMeta(node);
    } else {
      node.innerHTML = `<div class="vs-ingestion-progress-copy" aria-live="polite"><span data-ingestion-message></span></div><div class="vs-ingestion-progress-track" data-ingestion-track><div class="vs-ingestion-progress-bar" data-ingestion-bar></div></div><button type="button" class="vs-ingestion-progress-retry" data-ingestion-retry hidden>Retry</button>`;
    }
    node.dataset.ingestionReady = "1";
    bindRetry(node);
  };

  const stageIndex = (stage) => ({ transcribing: 1, finding_clips: 2, creating_clip: 3, ready: 4 }[stage] ?? 0);

  const updateSteps = (node, stage, failed, autoRenderStatus = "") => {
    if (!node.hasAttribute("data-ingestion-card")) return;
    const index = stageIndex(stage);
    node.querySelectorAll("[data-ingestion-step]").forEach((step, stepIndex) => {
      const isFirstClip = stepIndex === 3;
      const readyFirstClip = stage === "ready" && isFirstClip;
      const skipped = readyFirstClip && !failed && !["done", "failed"].includes(autoRenderStatus);
      const stepFailed = (readyFirstClip && autoRenderStatus === "failed") || (failed && stepIndex === Math.min(index, 3));
      const done = !failed && (
        (stage === "ready" && !isFirstClip)
        || (readyFirstClip && autoRenderStatus === "done")
        || (stage !== "ready" && stepIndex < index)
      );
      step.classList.toggle("is-done", done);
      step.classList.toggle("is-active", !failed && stage !== "ready" && stepIndex === index);
      step.classList.toggle("is-failed", stepFailed);
      step.classList.toggle("is-skipped", skipped);
    });
  };

  const rowForNode = (node) => {
    const id = String(node.dataset.videoPk || "");
    return Array.from(document.querySelectorAll("[data-video-row]")).find((row) => String(row.dataset.videoId || "") === id);
  };

  const ringIconForStage = (stage, node) => {
    if (stage === "uploading") return "bi-upload";
    if (stage === "downloading") return "bi-download";
    if (stage === "transcribing") return "bi-mic";
    if (stage === "finding_clips") return "bi-stars";
    if (stage === "creating_clip") return "bi-camera-reels";
    if (stage === "processing") return node.dataset.sourceKind === "upload" ? "bi-upload" : "bi-gear";
    return "bi-gear";
  };

  const ringLabelForStage = (stage, copy) => ({
    uploading: "Uploading",
    downloading: "Downloading from YouTube",
    transcribing: "Transcribing",
    finding_clips: "Finding the best clips",
    creating_clip: "Creating your first clip",
    processing: "Processing",
  }[stage] || copy || "Processing");

  const updateRing = (node, { stage, hasPercent, percentValue, failed, ready, autoRenderStatus, copy }) => {
    const ring = node.querySelector("[data-ingestion-ring]");
    if (!ring) return;
    const value = ring.querySelector("[data-ingestion-ring-value]");
    const percent = ring.querySelector("[data-ingestion-ring-percent]");
    const icon = ring.querySelector("[data-ingestion-ring-icon]");
    const label = ring.querySelector("[data-ingestion-ring-label]");
    const autoRenderFailed = autoRenderStatus === "failed";
    const skipped = ready && autoRenderStatus === "skipped";
    const ringFailed = failed || autoRenderFailed;
    const done = ready && !ringFailed && !skipped;
    const determinate = !ringFailed && !done && !skipped && hasPercent;
    const indeterminate = !ringFailed && !done && !skipped && !hasPercent;
    ring.classList.toggle("is-failed", ringFailed);
    ring.classList.toggle("is-done", done);
    ring.classList.toggle("is-skipped", skipped);
    ring.classList.toggle("is-determinate", determinate);
    ring.classList.toggle("is-indeterminate", indeterminate);
    const clamped = Math.max(0, Math.min(100, percentValue));
    if (value) value.style.strokeDashoffset = determinate ? String(100 - clamped) : "0";
    if (percent) percent.textContent = determinate ? `${Math.round(clamped)}%` : (done ? "✓" : (ringFailed ? "!" : (skipped ? "–" : "")));
    if (icon) {
      icon.className = `bi ${ringIconForStage(stage, node)}`;
      icon.hidden = !indeterminate;
    }
    if (label) {
      label.textContent = ringFailed
        ? "Processing failed"
        : (skipped ? "First clip skipped" : (done ? "Ready" : `${ringLabelForStage(stage, copy)}${determinate ? ` ${Math.round(clamped)} percent` : ""}`));
    }
  };

  const updateNode = (node, status = {}) => {
    ensureContents(node);
    const message = node.querySelector("[data-ingestion-message]");
    const bar = node.querySelector("[data-ingestion-bar]");
    const track = node.querySelector("[data-ingestion-track]");
    const activeRow = node.querySelector("[data-ingestion-active-row]");
    const retry = node.querySelector("[data-ingestion-retry]");
    const pill = rowForNode(node)?.querySelector("[data-ingestion-pill]") || node.closest(".mv-status-cell")?.querySelector("[data-ingestion-pill]");
    const stage = String(status.stage || "processing");
    const percentValue = Number(status.percent);
    const hasPercent = status.percent !== null && status.percent !== "" && Number.isFinite(percentValue) && percentValue >= 0;
    const isActive = Boolean(status.active);
    const failed = Boolean(status.failed);
    const ready = Boolean(status.ready ?? (stage === "ready" && !isActive && !failed));
    const autoRenderStatus = String(status.auto_render_status || "");
    const autoRenderFailed = autoRenderStatus === "failed";
    const eta = formatEta(status.eta_seconds);
    const renderEta = formatRenderEta(status.render_eta_seconds);
    let copy = String((ready && status.detail_message) || status.message || (ready ? "Ready" : "Processing…"));
    if (hasPercent && stage === "downloading" && !copy.includes("%")) copy += ` ${Math.round(percentValue)}%`;
    if (eta && stage === "downloading" && !copy.includes("left")) copy += ` · ${eta}`;
    if (renderEta && stage === "creating_clip" && !copy.includes(renderEta)) copy += ` · ${renderEta}`;
    if (failed && status.error) copy = String(status.error);
    if (message) message.textContent = copy;
    node.classList.toggle("is-failed", failed);
    node.classList.toggle("is-auto-render-failed", autoRenderFailed);
    node.classList.toggle("is-ready", ready);
    node.classList.toggle("is-indeterminate", isActive && !hasPercent);
    if (track) track.hidden = !isActive;
    if (activeRow) activeRow.hidden = ready && !status.detail_message;
    if (bar) bar.style.width = hasPercent ? `${Math.max(0, Math.min(100, percentValue))}%` : "0%";
    if (retry) retry.hidden = (!failed && !autoRenderFailed) || !node.dataset.retryUrl;
    node.querySelector("[data-ingestion-ready]")?.toggleAttribute("hidden", !ready);
    const readyText = node.querySelector("[data-ingestion-ready-text]");
    if (readyText) readyText.textContent = status.ready_text || "Ready ✓";
    updateRing(node, { stage, hasPercent, percentValue, failed, ready, autoRenderStatus, copy });
    updateSteps(node, failed && node.dataset.lastStage ? node.dataset.lastStage : stage, failed, autoRenderStatus);
    node.dataset.wasActive = isActive ? "1" : node.dataset.wasActive || "0";
    node.dataset.lastStage = stage;
    node.dataset.lastSuccessfulPollAt = String(Date.now());
    if (!node.hasAttribute("data-ingestion-card")) node.hidden = ready;
    if (pill) {
      pill.textContent = failed ? "Failed" : (isActive ? "Processing" : "Ready");
      pill.className = `mv-status-pill mv-status-pill--${failed ? "failed" : (isActive ? "processing" : "ready")}`;
    }
    if (
      ready
      && autoRenderStatus !== "failed"
      && node.hasAttribute("data-ingestion-card")
      && node.dataset.autoOpen === "1"
      && node.dataset.openUrl
      && !autoOpenClaimed
    ) {
      autoOpenClaimed = true;
      node.dataset.autoOpen = "0";
      window.setTimeout(() => window.location.assign(node.dataset.openUrl), 250);
    }
  };

  const showConnectionState = () => {
    nodes().filter((node) => node.dataset.wasActive === "1").forEach((node) => {
      const lastPoll = Number(node.dataset.lastSuccessfulPollAt || 0);
      if (lastPoll && Date.now() - lastPoll < 60000) return;
      ensureContents(node);
      node.hidden = false;
      node.classList.add("is-indeterminate");
      const message = node.querySelector("[data-ingestion-message]");
      if (message) message.textContent = "Still working…";
    });
  };

  const groups = () => {
    const result = new Map();
    nodes().forEach((node) => {
      const url = String(node.dataset.statusUrl || "");
      if (!url) return;
      if (!result.has(url)) result.set(url, []);
      result.get(url).push(node);
    });
    return result;
  };

  const poll = async () => {
    if (requestRunning || document.hidden) return;
    requestRunning = true;
    let anyActive = false;
    try {
      for (const [baseUrl, groupNodes] of groups()) {
        const ids = groupNodes.map((node) => node.dataset.videoPk).filter(Boolean);
        if (!ids.length) continue;
        const url = new URL(baseUrl, window.location.href);
        url.searchParams.set("video_ids", ids.join(","));
        try {
          const response = await fetch(url.toString(), { credentials: "same-origin", cache: "no-store", headers: { "X-Requested-With": "XMLHttpRequest" } });
          const payload = await response.json().catch(() => ({}));
          if (response.status >= 400 && response.status < 500) {
            groupNodes.forEach((node) => updateNode(node, { stage: "failed", failed: true, error: payload.message || "Progress is unavailable." }));
            continue;
          }
          if (!response.ok || !payload.ok) throw new Error(payload.message || "Progress check failed.");
          [...groupNodes].sort((left, right) => {
            const leftOrder = Number(left.dataset.autoOpenOrder || Number.MAX_SAFE_INTEGER);
            const rightOrder = Number(right.dataset.autoOpenOrder || Number.MAX_SAFE_INTEGER);
            return leftOrder - rightOrder;
          }).forEach((node) => {
            const status = payload.statuses?.[String(node.dataset.videoPk)];
            if (!status) return;
            anyActive = anyActive || Boolean(status.active);
            updateNode(node, status);
          });
        } catch (_) {
          showConnectionState();
          anyActive = anyActive || groupNodes.some((node) => node.dataset.wasActive === "1");
        }
      }
    } finally {
      requestRunning = false;
      if (!anyActive && timer) {
        window.clearInterval(timer);
        timer = null;
      }
    }
  };

  const wakePolling = () => {
    void poll();
    if (!timer) timer = window.setInterval(poll, POLL_MS);
  };

  function bindRetry(node) {
    const retry = node.querySelector("[data-ingestion-retry]");
    if (!retry || retry.dataset.bound === "1") return;
    retry.dataset.bound = "1";
    retry.addEventListener("click", async () => {
      const retryUrl = String(node.dataset.retryUrl || "");
      if (!retryUrl) return;
      retry.disabled = true;
      const message = node.querySelector("[data-ingestion-message]");
      if (message) message.textContent = "Restarting…";
      try {
        const response = await fetch(retryUrl, { method: "POST", credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload.ok) throw new Error(payload.message || "Retry failed.");
        node.dataset.wasActive = "1";
        updateNode(node, payload.status || { stage: "processing", active: true, message: "Processing…" });
        wakePolling();
      } catch (error) {
        if (message) message.textContent = error.message || "Retry failed.";
      } finally {
        retry.disabled = false;
      }
    });
  }

  const cardStack = () => document.querySelector("[data-ingestion-card-stack]");
  const addCard = (options = {}) => {
    const stack = cardStack();
    if (!stack) return null;
    const videoPk = String(options.videoPk || "");
    const clientId = String(options.clientId || "");
    let node = nodes().find((candidate) => (videoPk && candidate.dataset.videoPk === videoPk) || (clientId && candidate.dataset.clientId === clientId));
    if (!node) {
      node = document.createElement("article");
      node.className = "vs-ingestion-card";
      node.setAttribute("data-ingestion-card", "");
      stack.prepend(node);
    }
    if (videoPk) node.dataset.videoPk = videoPk;
    if (clientId) node.dataset.clientId = clientId;
    node.dataset.statusUrl = options.statusUrl || node.dataset.statusUrl || stack.dataset.statusUrl || "";
    node.dataset.retryUrl = options.retryUrl || node.dataset.retryUrl || "";
    node.dataset.openUrl = options.openUrl || node.dataset.openUrl || "";
    node.dataset.videoTitle = options.title || node.dataset.videoTitle || "Video";
    node.dataset.videoThumbnail = options.thumbnail || node.dataset.videoThumbnail || "";
    node.dataset.videoDuration = options.duration || node.dataset.videoDuration || "";
    node.dataset.videoChannel = options.channel || node.dataset.videoChannel || "";
    node.dataset.sourceKind = options.sourceKind || node.dataset.sourceKind || "url";
    if (options.autoOpen === true && node.dataset.autoOpen !== "1") {
      autoOpenSequence += 1;
      node.dataset.autoOpen = "1";
      node.dataset.autoOpenOrder = String(autoOpenSequence);
    }
    node.dataset.wasActive = "1";
    node.removeAttribute("hidden");
    node.dataset.ingestionReady = "0";
    ensureContents(node);
    updateNode(node, options.status || { stage: node.dataset.sourceKind === "upload" ? "uploading" : "downloading", active: true, message: node.dataset.sourceKind === "upload" ? "Uploading…" : "Downloading from YouTube…" });
    if (videoPk) wakePolling();
    return node;
  };

  const updatePending = (clientId, status) => {
    const node = nodes().find((candidate) => candidate.dataset.clientId === String(clientId));
    if (node) updateNode(node, status);
  };

  const promotePending = (clientId, options = {}) => {
    const node = addCard({ ...options, clientId });
    if (!node) return null;
    node.dataset.videoPk = String(options.videoPk || "");
    node.dataset.statusUrl = options.statusUrl || "";
    node.dataset.retryUrl = options.retryUrl || "";
    node.dataset.openUrl = options.openUrl || "";
    const open = node.querySelector("[data-ingestion-open]");
    if (open) open.href = node.dataset.openUrl || "#";
    updateNode(node, options.status || { stage: "transcribing", active: true, message: "Transcribing…" });
    wakePolling();
    return node;
  };

  window.MintiIngestionProgress = { addCard, updatePending, promotePending, wakePolling };
  nodes().forEach((node) => {
    ensureContents(node);
    if (node.hasAttribute("data-ingestion-card")) {
      updateNode(node, {
        stage: node.dataset.initialStage || "processing",
        active: node.dataset.initialFailed !== "1",
        failed: node.dataset.initialFailed === "1",
        message: node.dataset.initialFailed === "1" ? "Processing failed" : "Processing…",
        error: node.dataset.initialError || "",
      });
    }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) wakePolling(); });
  window.addEventListener("focus", wakePolling);
  wakePolling();
  window.addEventListener("pagehide", () => window.clearInterval(timer), { once: true });
})();
