(() => {
  const pluralized = (count, singular, plural) => `${count} ${count === 1 ? singular : plural}`;

  const deletionCopy = (clipCount, suggestionCount) => {
    const parts = [];
    if (clipCount > 0) parts.push(pluralized(clipCount, 'clip', 'clips'));
    if (suggestionCount > 0) parts.push(pluralized(suggestionCount, 'suggestion', 'suggestions'));
    if (!parts.length) return 'This video will be deleted.';
    return `This will delete ${parts.join(' and ')}.`;
  };

  const durationLabel = (rawSeconds) => {
    const total = Math.max(0, Math.round(Number(rawSeconds) || 0));
    if (!total) return '—';
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    return hours
      ? `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
      : `${minutes}:${String(seconds).padStart(2, '0')}`;
  };

  const addedDateLabel = (rawValue) => {
    if (!rawValue) return 'Unknown date';
    const parsed = new Date(rawValue);
    if (Number.isNaN(parsed.getTime())) return String(rawValue);
    return new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    }).format(parsed);
  };

  document.querySelectorAll('[data-delete-video-modal]').forEach((modalEl) => {
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    const summaryEl = modalEl.querySelector('[data-delete-summary]');
    const scheduledEl = modalEl.querySelector('[data-delete-scheduled]');
    const youtubeEl = modalEl.querySelector('[data-delete-youtube-warning]');
    const errorEl = modalEl.querySelector('[data-delete-error]');
    const confirmButton = modalEl.querySelector('[data-delete-confirm]');
    const titleEl = modalEl.querySelector('[data-delete-video-title]');
    const detailEl = modalEl.querySelector('[data-delete-video-detail]');
    const thumbnailEl = modalEl.querySelector('[data-delete-video-thumbnail]');
    const thumbnailPlaceholder = modalEl.querySelector('[data-delete-video-thumbnail-placeholder]');
    let pendingDelete = null;

    const showError = (message = '') => {
      errorEl.textContent = message;
      errorEl.classList.toggle('d-none', !message);
    };

    const setThumbnail = (url, title) => {
      const cleanUrl = String(url || '').trim();
      thumbnailEl.alt = title ? `Thumbnail for ${title}` : 'Video thumbnail';
      thumbnailEl.classList.toggle('d-none', !cleanUrl);
      thumbnailPlaceholder.classList.toggle('d-none', Boolean(cleanUrl));
      if (cleanUrl) thumbnailEl.src = cleanUrl;
      else thumbnailEl.removeAttribute('src');
    };

    const setVideoMeta = (details = {}, trigger = {}) => {
      const title = String(details.title || trigger.videoTitle || 'Untitled video');
      titleEl.textContent = title;
      titleEl.title = title;
      const duration = trigger.videoDuration || durationLabel(trigger.videoDurationSeconds || details.duration_seconds);
      const added = trigger.videoAdded || addedDateLabel(details.added_at);
      detailEl.textContent = `${duration || '—'} · Added ${added || 'Unknown date'}`;
      setThumbnail(trigger.videoThumbnail || details.thumbnail_url, title);
    };

    document.querySelectorAll('[data-delete-video]').forEach((button) => {
      button.addEventListener('click', async () => {
        pendingDelete = {
          deleteUrl: button.dataset.deleteUrl || '',
          summaryUrl: button.dataset.summaryUrl || '',
          trigger: button.dataset,
        };
        setVideoMeta({}, button.dataset);
        summaryEl.textContent = 'Loading deletion details…';
        scheduledEl.classList.add('d-none');
        youtubeEl.classList.add('d-none');
        confirmButton.disabled = true;
        showError('');
        modal.show();
        try {
          const response = await fetch(pendingDelete.summaryUrl, { credentials: 'same-origin' });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok || !payload.ok) throw new Error(payload.message || 'Deletion details could not be loaded.');
          const details = payload.summary || {};
          const clips = Number(details.clip_count || 0);
          const suggestions = Number(details.suggestion_count || 0);
          const scheduled = Number(details.scheduled_post_count || 0);
          const youtube = Number(details.remote_youtube_schedule_count || 0);
          setVideoMeta(details, pendingDelete.trigger);
          summaryEl.textContent = deletionCopy(clips, suggestions);
          scheduledEl.textContent = `${pluralized(scheduled, 'scheduled post', 'scheduled posts')} will be cancelled.`;
          scheduledEl.classList.toggle('d-none', scheduled === 0);
          youtubeEl.textContent = `${youtube} ${youtube === 1 ? 'clip is' : 'clips are'} already scheduled on YouTube. We can't remove ${youtube === 1 ? 'it' : 'them'} there; delete ${youtube === 1 ? 'it' : 'them'} in YouTube Studio if you don't want ${youtube === 1 ? 'it' : 'them'} to publish.`;
          youtubeEl.classList.toggle('d-none', youtube === 0);
          confirmButton.disabled = false;
        } catch (error) {
          showError(error.message || 'Deletion details could not be loaded.');
        }
      });
    });

    confirmButton.addEventListener('click', async () => {
      if (!pendingDelete?.deleteUrl) return;
      confirmButton.disabled = true;
      showError('');
      try {
        const response = await fetch(pendingDelete.deleteUrl, {
          method: 'POST',
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
          credentials: 'same-origin',
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload.ok) throw new Error(payload.message || 'Video could not be deleted.');
        window.location.assign(payload.redirect_url || modalEl.dataset.deleteFallbackUrl);
      } catch (error) {
        showError(error.message || 'Video could not be deleted.');
        confirmButton.disabled = false;
      }
    });
  });
})();
