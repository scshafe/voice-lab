'use strict';

(() => {
  const TOKEN_PATTERN = /^[0-9a-f]{64}$/;
  const POLL_INTERVAL_MS = 500;
  const RETAKE_REASON = 'retake requested from capture console';
  const FINDING_HINTS = Object.freeze({
    'auto-stopped': 'the ten-minute safety limit stopped this take; record it again',
    'clipped-samples': 'audio clipped; move farther away or speak more softly, then retake',
    'dc-offset': 'the signal has a DC offset; check the microphone connection, then retake',
    'digital-silence-or-dropout': 'silence or a dropout was detected; check the connection, then retake',
    'peak-too-quiet': 'move closer or speak up, then retake',
  });

  const elements = Object.freeze({
    accept: document.querySelector('#accept'),
    announcer: document.querySelector('#announcer'),
    captureView: document.querySelector('#capture-view'),
    copySync: document.querySelector('#copy-sync'),
    error: document.querySelector('#error'),
    finalView: document.querySelector('#final-view'),
    finalize: document.querySelector('#finalize'),
    findings: document.querySelector('#findings'),
    manifestDigest: document.querySelector('#manifest-digest'),
    metricClipped: document.querySelector('#metric-clipped'),
    metricDuration: document.querySelector('#metric-duration'),
    metricPeak: document.querySelector('#metric-peak'),
    metricSnr: document.querySelector('#metric-snr'),
    play: document.querySelector('#play'),
    preview: document.querySelector('#preview'),
    progress: document.querySelector('#progress'),
    promptInstruction: document.querySelector('#prompt-instruction'),
    promptMeta: document.querySelector('#prompt-meta'),
    promptText: document.querySelector('#prompt-text'),
    qcBadge: document.querySelector('#qc-badge'),
    qcDigest: document.querySelector('#qc-digest'),
    retake: document.querySelector('#retake'),
    roomToneBanner: document.querySelector('#room-tone-banner'),
    shutdown: document.querySelector('#shutdown'),
    start: document.querySelector('#start'),
    statePill: document.querySelector('#state-pill'),
    stop: document.querySelector('#stop'),
    summary: document.querySelector('#take-summary'),
    summaryView: document.querySelector('#summary-view'),
    syncCommand: document.querySelector('#sync-command'),
    takeReadout: document.querySelector('#take-readout'),
  });

  const token = window.location.hash.slice(1);
  let state = null;
  let pollTimer = null;
  let previewObjectUrl = null;
  let busy = false;
  let lastAnnouncementKey = '';

  const announce = (message) => {
    elements.announcer.textContent = '';
    window.setTimeout(() => {
      elements.announcer.textContent = message;
    }, 0);
  };

  const showError = (message) => {
    elements.error.textContent = message;
    elements.error.hidden = false;
    announce(message);
  };

  const clearError = () => {
    elements.error.textContent = '';
    elements.error.hidden = true;
  };

  const responseError = async (response) => {
    let detail = '';
    try {
      const payload = await response.json();
      detail = payload.error || payload.detail || '';
    } catch (_error) {
      detail = '';
    }
    return detail || `request failed with status ${response.status}`;
  };

  const request = async (path, options = {}) => {
    const headers = new Headers(options.headers || {});
    if (!headers.has('Accept')) {
      headers.set('Accept', 'application/json');
    }
    headers.set('X-Capture-Token', token);
    const response = await fetch(path, {
      ...options,
      cache: 'no-store',
      credentials: 'same-origin',
      headers,
    });
    if (!response.ok) {
      throw new Error(await responseError(response));
    }
    return response;
  };

  const getState = async () => {
    const response = await request('/api/state');
    return response.json();
  };

  const post = async (path, payload = {}) => request(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });

  const fixed = (value, digits = 1) => (
    typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
  );

  const elapsedLabel = (value) => `${fixed(value, 1)} s`;

  const phaseLabel = (captureState) => {
    const labels = {
      complete: 'Complete',
      finalized: 'READY published',
      ready: 'Ready',
      recording: `Recording · ${elapsedLabel(captureState.elapsed_seconds)}`,
      review: 'Review',
    };
    return labels[captureState.status] || 'Unavailable';
  };

  const setBusy = (value) => {
    busy = value;
    for (const button of [
      elements.accept,
      elements.copySync,
      elements.finalize,
      elements.play,
      elements.retake,
      elements.shutdown,
      elements.start,
      elements.stop,
    ]) {
      button.disabled = value;
    }
    if (!value && state) {
      elements.finalize.disabled = state.status !== 'complete';
    }
  };

  const clearPreview = () => {
    elements.preview.pause();
    elements.preview.removeAttribute('src');
    elements.preview.load();
    elements.preview.hidden = true;
    if (previewObjectUrl !== null) {
      URL.revokeObjectURL(previewObjectUrl);
      previewObjectUrl = null;
    }
  };

  const makeCell = (text, className = '') => {
    const cell = document.createElement('td');
    cell.textContent = text;
    if (className) {
      cell.className = className;
    }
    return cell;
  };

  const qcSummary = (take) => {
    if (!take.metrics) {
      return {label: '—', status: 'pending'};
    }
    const findings = Array.isArray(take.findings) ? take.findings : [];
    if (take.metrics.passed && findings.length === 0) {
      return {label: 'Pass', status: 'pass'};
    }
    return {
      label: findings.length > 0 ? `Review — ${findings.join(', ')}` : 'Review',
      status: 'review',
    };
  };

  const renderSummary = (takes) => {
    elements.summary.replaceChildren();
    for (const take of takes) {
      const row = document.createElement('tr');
      const prompt = take.prompt || {};
      const qc = qcSummary(take);
      const qcCell = makeCell(qc.label, 'status-text');
      qcCell.dataset.status = qc.status;
      row.append(
        makeCell(`${Number(take.index) + 1} of ${take.total}`),
        makeCell(`${prompt.prompt_id || 'Unknown'} · ${prompt.category || take.kind || 'take'}`),
        makeCell(take.status || 'pending', 'status-text'),
        qcCell,
      );
      elements.summary.append(row);
    }
  };

  const renderFindings = (findings) => {
    elements.findings.replaceChildren();
    if (findings.length === 0) {
      const item = document.createElement('li');
      item.className = 'finding-pass';
      item.textContent = 'No automatic QC findings for this take.';
      elements.findings.append(item);
      return;
    }
    for (const finding of findings) {
      const item = document.createElement('li');
      const hint = FINDING_HINTS[finding] || 'listen carefully and retake if needed';
      item.textContent = `${finding}: ${hint}`;
      elements.findings.append(item);
    }
  };

  const renderMetrics = (captureState) => {
    const metrics = captureState.metrics;
    const findings = Array.isArray(captureState.findings) ? captureState.findings : [];
    elements.takeReadout.hidden = captureState.status !== 'review' || !metrics;
    if (!metrics) {
      return;
    }
    elements.metricDuration.textContent = `${fixed(metrics.duration_seconds, 2)} s`;
    elements.metricPeak.textContent = `${fixed(metrics.peak_dbfs, 1)} dBFS`;
    elements.metricClipped.textContent = String(metrics.clipped_samples);
    elements.metricSnr.textContent = metrics.signal_to_noise_db === null
      ? 'Not available for room tone'
      : `${fixed(metrics.signal_to_noise_db, 1)} dB`;
    const passed = metrics.passed && findings.length === 0;
    elements.qcBadge.textContent = passed ? 'Automatic QC pass' : 'Review suggested';
    elements.qcBadge.dataset.status = passed ? 'pass' : 'review';
    renderFindings(findings);
  };

  const primaryButton = (captureState) => {
    if (captureState.status === 'ready') {
      return elements.start;
    }
    if (captureState.status === 'recording') {
      return elements.stop;
    }
    if (captureState.status === 'review') {
      return elements.accept;
    }
    if (captureState.status === 'complete') {
      return elements.finalize;
    }
    if (captureState.status === 'finalized') {
      return elements.copySync;
    }
    return null;
  };

  const focusPrimary = (captureState) => {
    const primary = primaryButton(captureState);
    if (primary !== null && !primary.hidden && !primary.disabled) {
      primary.focus();
    }
  };

  const renderControls = (captureState) => {
    const review = captureState.status === 'review';
    elements.start.hidden = captureState.status !== 'ready';
    elements.stop.hidden = captureState.status !== 'recording';
    elements.play.hidden = !review;
    elements.retake.hidden = !review;
    elements.accept.hidden = !review;
    elements.finalize.disabled = busy || captureState.status !== 'complete';
  };

  const renderCapture = (captureState) => {
    const visible = ['ready', 'recording', 'review'].includes(captureState.status);
    elements.captureView.hidden = !visible;
    if (!visible || !captureState.prompt) {
      return;
    }
    const prompt = captureState.prompt;
    elements.promptMeta.textContent = `${prompt.category} · ${prompt.style}`;
    elements.promptText.textContent = prompt.text;
    elements.roomToneBanner.hidden = captureState.kind !== 'room-tone'
      || !['ready', 'recording'].includes(captureState.status);
    if (captureState.status === 'ready') {
      elements.promptInstruction.textContent = captureState.kind === 'room-tone'
        ? 'Get still and quiet, then start the room-tone take.'
        : 'Read the prompt exactly as printed. Do not improvise or correct it.';
    } else if (captureState.status === 'recording') {
      elements.promptInstruction.textContent = captureState.kind === 'room-tone'
        ? 'Stay silent. Press Space when the room-tone interval is complete.'
        : 'Recording through FFmpeg. Press Space when the take is complete.';
    } else {
      elements.promptInstruction.textContent = 'Play the preview, inspect the readout, then accept or retake.';
    }
  };

  const renderFinal = (captureState) => {
    const finalized = captureState.status === 'finalized' && captureState.ready === true;
    elements.finalView.hidden = !finalized;
    if (!finalized) {
      return;
    }
    elements.manifestDigest.textContent = captureState.manifest_sha256 || '—';
    elements.qcDigest.textContent = captureState.qc_report_sha256 || '—';
    elements.syncCommand.textContent = captureState.sync_command || '—';
  };

  const render = (captureState, {focus = false} = {}) => {
    state = captureState;
    const index = captureState.index === null ? captureState.total : Number(captureState.index) + 1;
    elements.progress.textContent = `${index} of ${captureState.total}`;
    elements.statePill.textContent = phaseLabel(captureState);
    elements.statePill.dataset.state = captureState.status;
    renderControls(captureState);
    renderCapture(captureState);
    renderMetrics(captureState);
    renderSummary(Array.isArray(captureState.takes) ? captureState.takes : []);
    elements.summaryView.hidden = !['complete', 'finalized'].includes(captureState.status);
    renderFinal(captureState);

    const announcementKey = `${captureState.status}:${captureState.index}`;
    if (announcementKey !== lastAnnouncementKey) {
      announce(`${phaseLabel(captureState)}. ${elements.progress.textContent}.`);
      lastAnnouncementKey = announcementKey;
    }
    if (focus) {
      focusPrimary(captureState);
    }
  };

  const schedulePoll = () => {
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
    if (state && state.status === 'recording' && !busy) {
      pollTimer = window.setTimeout(refreshState, POLL_INTERVAL_MS);
    }
  };

  async function refreshState(options = {}) {
    try {
      const captureState = await getState();
      const pollingAutoStop = !busy
        && state !== null
        && state.status === 'recording'
        && captureState.status === 'review';
      clearError();
      render(captureState, {focus: options.focus === true || pollingAutoStop});
    } catch (error) {
      showError(`Capture console unavailable: ${error.message}`);
    } finally {
      schedulePoll();
    }
  }

  const transition = async (path, payload = {}) => {
    if (busy) {
      return;
    }
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
    clearError();
    clearPreview();
    setBusy(true);
    try {
      await post(path, payload);
      await refreshState();
    } catch (error) {
      showError(`Action failed: ${error.message}`);
    } finally {
      setBusy(false);
      if (state !== null) {
        focusPrimary(state);
      }
      schedulePoll();
    }
  };

  const playPreview = async () => {
    if (busy || !state || state.status !== 'review') {
      return;
    }
    clearError();
    setBusy(true);
    let manualPlayback = false;
    try {
      const response = await request('/api/take/preview', {headers: {'Accept': 'audio/wav'}});
      const blob = await response.blob();
      clearPreview();
      previewObjectUrl = URL.createObjectURL(blob);
      elements.preview.src = previewObjectUrl;
      elements.preview.hidden = false;
      try {
        await elements.preview.play();
        announce('Playing the current take preview.');
      } catch (_playbackError) {
        manualPlayback = true;
        announce('Preview loaded. Press Play in the audio control to listen.');
      }
    } catch (error) {
      showError(`Preview failed: ${error.message}`);
    } finally {
      setBusy(false);
      if (manualPlayback) {
        elements.preview.focus();
      } else if (state && state.status === 'review') {
        elements.accept.focus();
      }
    }
  };

  const isEditableTarget = (target) => (
    target instanceof HTMLInputElement
    || target instanceof HTMLTextAreaElement
    || target instanceof HTMLSelectElement
    || (target instanceof HTMLElement && target.isContentEditable)
  );

  const copyText = async (value) => {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        return;
      } catch (_error) {
        // The local-page fallback below still requires an explicit click.
      }
    }
    const helper = document.createElement('textarea');
    helper.className = 'copy-helper';
    helper.value = value;
    helper.readOnly = true;
    document.body.append(helper);
    helper.select();
    const copied = document.execCommand('copy');
    helper.remove();
    if (!copied) {
      throw new Error('copy command was unavailable');
    }
  };

  elements.start.addEventListener('click', () => transition('/api/take/start'));
  elements.stop.addEventListener('click', () => transition('/api/take/stop'));
  elements.play.addEventListener('click', playPreview);
  elements.retake.addEventListener('click', () => (
    transition('/api/take/retake', {reason: RETAKE_REASON})
  ));
  elements.accept.addEventListener('click', () => transition('/api/take/accept'));
  elements.finalize.addEventListener('click', () => transition('/api/finalize'));
  elements.copySync.addEventListener('click', async () => {
    try {
      await copyText(elements.syncCommand.textContent);
      announce('Sync command copied.');
      elements.copySync.textContent = 'Copied';
    } catch (error) {
      showError(`Copy failed: ${error.message}`);
    }
  });
  elements.shutdown.addEventListener('click', async () => {
    if (busy) {
      return;
    }
    setBusy(true);
    try {
      await post('/api/shutdown');
      elements.shutdown.textContent = 'Console stopped';
      announce('Capture console stopped. You may close this tab.');
    } catch (error) {
      showError(`Shutdown failed: ${error.message}`);
      setBusy(false);
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.repeat || event.metaKey || event.ctrlKey || event.altKey || busy || !state) {
      return;
    }
    const target = event.target;
    if (target instanceof HTMLAudioElement || isEditableTarget(target)) {
      return;
    }
    const key = event.key.toLowerCase();
    if (event.code === 'Space' && ['ready', 'recording'].includes(state.status)) {
      event.preventDefault();
      transition(state.status === 'ready' ? '/api/take/start' : '/api/take/stop');
    } else if (key === 'p' && state.status === 'review') {
      event.preventDefault();
      playPreview();
    } else if (key === 'r' && state.status === 'review') {
      event.preventDefault();
      transition('/api/take/retake', {reason: RETAKE_REASON});
    } else if (
      event.key === 'Enter'
      && state.status === 'review'
      && !(target instanceof HTMLButtonElement)
    ) {
      event.preventDefault();
      transition('/api/take/accept');
    }
  });

  window.addEventListener('beforeunload', clearPreview);

  if (!TOKEN_PATTERN.test(token)) {
    showError('This capture-console link is missing its valid per-run token. Return to Terminal and use the printed link.');
    elements.statePill.textContent = 'Unavailable';
    elements.statePill.dataset.state = 'unavailable';
    return;
  }
  refreshState({focus: true});
})();
