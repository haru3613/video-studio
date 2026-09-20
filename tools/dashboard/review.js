(() => {
  'use strict';

  const KIND_LABELS = { video: '影片與切鏡', audio: '聲音與節奏', cover: '封面' };
  let session = null;
  let pendingController = null;
  let pendingContainer = null;
  let mountGeneration = 0;
  const draftCache = new Map();

  const escapeHTML = value => String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');

  const formatTime = value => {
    const seconds = Number(value);
    if (!Number.isFinite(seconds)) return '無時間點';
    const safe = Math.max(0, seconds);
    const minutes = Math.floor(safe / 60).toString().padStart(2, '0');
    const remainder = Math.floor(safe % 60).toString().padStart(2, '0');
    return `${minutes}:${remainder}`;
  };

  const finiteValue = value => value !== null && value !== undefined && Number.isFinite(Number(value))
    ? Number(value) : null;

  const apiPath = (source, project, suffix = '') =>
    `/api/review/${encodeURIComponent(source)}/${encodeURIComponent(project)}${suffix}`;

  function safeMediaURL(value) {
    if (!value) return '';
    try {
      const url = new URL(value, window.location.href);
      return url.origin === window.location.origin ? url.href : '';
    } catch (_) {
      return '';
    }
  }

  function newClientID() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map(byte => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  async function requestJSON(url, options = {}) {
    const response = await fetch(url, options);
    let payload = null;
    try { payload = await response.json(); } catch (_) { /* Error responses may be empty. */ }
    if (!response.ok) {
      const error = new Error(payload?.detail || payload?.error || `伺服器回應 ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function stopMedia(root = session?.root) {
    root?.querySelectorAll('video,audio').forEach(media => media.pause());
  }

  function rememberPlayback() {
    if (!session) return;
    const media = session.root.querySelector('[data-review-media]');
    if (media && Number.isFinite(media.currentTime)) {
      session.positions.set(media.dataset.reviewKey || session.selectedKey, media.currentTime);
    }
    stopMedia();
  }

  function assetKey(asset) {
    return `${asset?.id || ''}:${asset?.sha256 || ''}`;
  }

  function projectKey(source, project) {
    return `${source}\u0000${project}`;
  }

  function cacheDraft(active = session) {
    if (!active) return;
    const key = projectKey(active.source, active.project);
    if (!active.draft.trim()) {
      draftCache.delete(key);
      return;
    }
    draftCache.set(key, {
      draft: active.draft,
      draftContext: active.draftContext ? { ...active.draftContext } : null,
      clientId: active.clientId,
      stale: active.stale,
      needsRebind: active.needsRebind,
      writeMessage: active.writeMessage,
      packageId: active.data.package_id,
    });
  }

  function currentAsset(kind) {
    const assets = session.data.assets.filter(asset => asset.kind === kind);
    return assets.find(asset => asset.role === 'current') || assets[0] || null;
  }

  function findAsset(id, sha256) {
    return session.data.assets.find(asset => asset.id === id && (!sha256 || asset.sha256 === sha256)) || null;
  }

  function selectedAsset() {
    if (session.historicalAsset) return session.historicalAsset;
    return session.data.assets.find(asset => assetKey(asset) === session.selectedKey)
      || currentAsset(session.kind);
  }

  function assetVersion(asset) {
    if (!asset) return '素材不可用';
    const digest = asset.sha256 ? asset.sha256.slice(0, 8) : '無版本';
    return `${asset.label || asset.id || KIND_LABELS[asset.kind] || '素材'} · ${digest}`;
  }

  function snapshotContext(asset, timestamp) {
    return asset ? {
      assetId: asset.id,
      assetSha256: asset.sha256,
      assetLabel: asset.label || asset.id,
      assetKind: asset.kind,
      timestamp: Number.isFinite(timestamp) ? timestamp : null,
    } : null;
  }

  function mediaTime() {
    const media = session.root.querySelector('[data-review-media]');
    if (media && Number.isFinite(media.currentTime)) return media.currentTime;
    return session.positions.get(session.selectedKey) || 0;
  }

  function pinDraft(explicit = false) {
    if (session.draftContext) return;
    const asset = selectedAsset();
    if (!asset) return;
    stopMedia();
    session.draftContext = snapshotContext(asset, asset.kind === 'cover' ? null : mediaTime());
    session.draftExplicit = explicit;
    renderComposerContext();
  }

  function renderComposerContext() {
    const label = session.root.querySelector('#review-comment-context');
    if (!label) return;
    const context = session.draftContext;
    if (!context) {
      const asset = selectedAsset();
      label.textContent = session.historicalAsset
        ? '這是留言所屬舊版本；請先選擇目前素材再新增留言。'
        : asset ? `${assetVersion(asset)} · 開始輸入時固定時間點` : '請先選擇素材';
      return;
    }
    label.textContent = `${context.assetLabel} · ${context.assetSha256?.slice(0, 8) || '無版本'} · ${formatTime(context.timestamp)}`;
  }

  function mediaMarkup(asset) {
    const url = safeMediaURL(asset?.url);
    if (!asset || !url || !asset.sha256) {
      return '<div class="review-media-missing" role="status">這個素材目前無法播放。版本資料仍保留在下方。</div>';
    }
    const label = escapeHTML(asset.label || asset.id);
    if (asset.kind === 'cover') {
      return `<figure class="review-cover"><img src="${escapeHTML(url)}" alt="${label}" loading="lazy"><figcaption>${label}</figcaption></figure>`;
    }
    const tag = asset.kind === 'audio' ? 'audio' : 'video';
    const className = asset.kind === 'video' ? ' class="review-main-video"' : '';
    const cover = asset.kind === 'video' ? session.data.assets.find(item => item.kind === 'cover'
      && item.role === 'current' && item.source === asset.source && item.project === asset.project) : null;
    const posterURL = safeMediaURL(cover?.url);
    const poster = posterURL ? ` poster="${escapeHTML(posterURL)}"` : '';
    return `<${tag}${className} data-review-media data-review-key="${escapeHTML(assetKey(asset))}" controls playsinline preload="metadata" src="${escapeHTML(url)}"${poster} aria-label="${label}"></${tag}>`;
  }

  function versionPicker(kind) {
    const assets = session.data.assets.filter(asset => asset.kind === kind);
    if (assets.length < 2) return '';
    return `<div class="review-versions" aria-label="${escapeHTML(KIND_LABELS[kind])}版本">${assets.map((asset, index) => {
      const selected = !session.historicalAsset && assetKey(asset) === session.selectedKey;
      const prefix = kind === 'audio' && index < 2 ? `${String.fromCharCode(65 + index)} · ` : '';
      return `<button type="button" data-version-key="${escapeHTML(assetKey(asset))}" aria-pressed="${selected}">${escapeHTML(prefix + (asset.label || asset.id))}</button>`;
    }).join('')}</div>`;
  }

  function materialMarkup() {
    const asset = selectedAsset();
    if (!asset) return '<div class="review-media-missing">這類素材目前不存在。</div>';
    const position = session.positions.get(session.selectedKey) || 0;
    const duration = finiteValue(asset.duration_seconds);
    const end = duration !== null ? ` / ${formatTime(duration)}` : '';
    const historical = session.historicalAsset ? '<span class="review-old-badge">留言所屬舊版本</span>' : '';
    const chapters = asset.kind === 'video' && session.data.chapters?.length
      ? `<div class="review-chapters" aria-label="影片章節">${session.data.chapters.map(chapter => `<button type="button" data-seek="${Number(chapter.seconds) || 0}"><b>${formatTime(chapter.seconds)}</b>${escapeHTML(chapter.title)}</button>`).join('')}</div>` : '';
    const commentButton = session.historicalAsset
      ? '<button type="button" disabled>舊版本僅供查看</button>'
      : `<button type="button" class="review-primary" data-action="comment-now">${asset.kind === 'cover' ? '對這張封面留言' : '在這一刻留言'}</button>`;
    const controls = asset.kind !== 'cover'
      ? `<div class="review-transport"><span id="review-clock" class="review-time">${formatTime(position)}${end}</span><button type="button" data-action="back5">−5 秒</button><button type="button" data-action="forward5">＋5 秒</button>${commentButton}</div>`
      : `<div class="review-transport">${commentButton}</div>`;
    return `${versionPicker(asset.kind)}<div class="review-player">${mediaMarkup(asset)}</div><div class="review-player-caption"><span>${escapeHTML(assetVersion(asset))}</span>${historical}</div>${controls}${chapters}`;
  }

  function changesMarkup() {
    const changes = Array.isArray(session.data.changes) ? session.data.changes : [];
    if (!changes.length) return '';
    return `<section class="review-panel review-change-panel"><div class="review-panel-head"><h2>這次改了什麼</h2></div><div class="review-change-list">${changes.map(change => {
      const asset = change.asset_id ? session.data.assets.find(item => item.id === change.asset_id) : null;
      const shortcut = asset ? `<button type="button" data-change-asset="${escapeHTML(assetKey(asset))}">查看素材 ↗</button>` : '';
      return `<div class="review-change"><strong>${escapeHTML(change.title)}</strong><p>${escapeHTML(change.description)}</p>${shortcut}</div>`;
    }).join('')}</div></section>`;
  }

  function historyMarkup() {
    return `<details class="review-panel review-history"><summary>版本與參考資料</summary><div class="review-history-body">${session.data.assets.map(asset => `<div class="review-history-row"><span>${escapeHTML(asset.label || asset.id)}</span><span>${escapeHTML(asset.role || 'reference')} · ${escapeHTML(asset.sha256?.slice(0, 8) || '素材缺失')}${finiteValue(asset.duration_seconds) !== null ? ` · ${formatTime(asset.duration_seconds)}` : ''}</span></div>`).join('')}<p>每則回饋都保留當時的素材與版本。舊檔不存在時，留言仍會保留。</p></div></details>`;
  }

  function commentMarkup(comment) {
    const asset = comment.asset || {};
    const available = comment.asset_available !== false && Boolean(safeMediaURL(asset.url));
    const timestamp = comment.timestamp_seconds;
    const location = timestamp !== null && timestamp !== undefined && Number.isFinite(Number(timestamp)) ? formatTime(timestamp) : '無時間點';
    const version = asset.sha256 ? asset.sha256.slice(0, 8) : '無版本';
    const old = comment.is_current === false ? '<span class="review-old-badge">舊版本</span>' : '';
    const locationControl = available
      ? `<button type="button" data-comment-id="${escapeHTML(comment.id)}" aria-label="前往 ${escapeHTML(asset.label || asset.id)} ${location}">${escapeHTML(location)}</button>`
      : `<span class="review-unavailable">${escapeHTML(location)} · 檔案已不可用</span>`;
    const next = comment.status === 'resolved' ? 'open' : 'resolved';
    return `<article class="review-thread ${comment.status === 'resolved' ? 'is-resolved' : ''}"><div class="review-thread-meta">${locationControl}<span>${escapeHTML(asset.label || asset.id || '未知素材')} · ${escapeHTML(version)}</span>${old}</div><p>${escapeHTML(comment.body)}</p><div class="review-thread-bottom"><span>${comment.status === 'resolved' ? '已處理' : '待處理'}</span><button type="button" data-resolve-id="${escapeHTML(comment.id)}" data-resolve-status="${next}">${next === 'resolved' ? '標記已處理' : '重新開啟'}</button></div></article>`;
  }

  function commentsMarkup() {
    const comments = session.data.comments.filter(comment => !session.onlyOpen || comment.status !== 'resolved');
    const openCount = session.data.comments.filter(comment => comment.status !== 'resolved').length;
    const staleActions = session.stale
      ? `<div class="review-stale-actions"><button type="button" data-action="refresh-review">重新載入版本</button>${session.needsRebind ? '<button type="button" data-action="rebind-draft">改綁目前素材</button>' : ''}</div>` : '';
    const historyReadOnly = Boolean(session.historicalAsset && !session.draftContext);
    const placeholder = historyReadOnly ? '這是留言所屬舊版本；請先選擇上方目前版本再新增留言。' : '哪裡需要調整？也可以記下你想保留的地方。';
    return `<section class="review-panel review-comments"><div class="review-panel-head"><h2>審片留言 <span>${openCount} 待處理</span></h2><button type="button" data-action="filter" aria-pressed="${session.onlyOpen}">${session.onlyOpen ? '只看待處理' : '全部留言'}</button></div><div id="review-threads">${comments.length ? comments.map(commentMarkup).join('') : '<div class="review-empty">這一輪沒有待處理留言。</div>'}</div><div class="review-composer"><div class="review-composer-label" id="review-comment-context"></div><textarea id="review-draft" aria-label="審片意見" placeholder="${placeholder}" ${historyReadOnly ? 'disabled' : ''}>${escapeHTML(session.draft)}</textarea>${staleActions}<div class="review-composer-footer"><span id="review-write-status" class="${session.stale ? 'is-error' : ''}" role="status" aria-live="polite">${escapeHTML(session.writeMessage || '')}</span><button type="button" class="review-primary" data-action="save-comment" ${historyReadOnly || session.needsRebind ? 'disabled' : ''}>加入留言</button></div></div></section>`;
  }

  function draw(playAfterLoad = false) {
    if (!session) return;
    rememberPlayback();
    session.mediaController?.abort();
    session.mediaController = new AbortController();
    const kinds = ['video', 'audio', 'cover'].filter(kind => session.data.assets.some(asset => asset.kind === kind));
    if (!kinds.includes(session.kind)) session.kind = kinds[0];
    const current = selectedAsset() || currentAsset(session.kind);
    if (current && !session.selectedKey) session.selectedKey = assetKey(current);
    session.root.innerHTML = `<div class="review-hub"><a class="review-back" href="#">← 專案總覽</a><div class="review-head"><div><div class="review-kicker">本集審片</div><h1>${escapeHTML(session.data.project.title || session.data.project.name)}</h1><div class="review-meta"><span>${escapeHTML(session.data.project.source)}</span><span>${escapeHTML(session.data.project.name)}</span></div></div><div class="review-head-actions"><button type="button" data-action="show-files">流程與檔案</button><a class="review-button" href="${escapeHTML(apiPath(session.source, session.project, '/export'))}">匯出回饋</a></div></div>${session.data.warnings?.length ? `<div class="review-warning" role="status">${session.data.warnings.map(escapeHTML).join(' · ')}</div>` : ''}<div class="review-layout"><div><div class="review-shortcuts">${(session.data.changes || []).filter(change => change.asset_id && findAsset(change.asset_id)).map(change => `<button type="button" data-change-asset="${escapeHTML(assetKey(findAsset(change.asset_id)))}">${escapeHTML(change.title)} ↗</button>`).join('')}</div><section class="review-panel"><div class="review-tabs" role="tablist" aria-label="審閱素材">${kinds.map(kind => `<button type="button" role="tab" aria-selected="${session.kind === kind}" data-kind="${kind}">${KIND_LABELS[kind]}</button>`).join('')}</div><div id="review-material">${materialMarkup()}</div></section>${changesMarkup()}${historyMarkup()}</div>${commentsMarkup()}</div><div id="review-toast" class="review-toast" role="status" aria-live="polite" hidden></div></div>`;
    wireMedia(playAfterLoad);
    renderComposerContext();
  }

  function updateComments() {
    if (!session) return;
    const current = session.root.querySelector('.review-comments');
    if (!current) return;
    const template = document.createElement('template');
    template.innerHTML = commentsMarkup().trim();
    current.replaceWith(template.content.firstElementChild);
    renderComposerContext();
  }

  function showToast(message) {
    const toast = session?.root.querySelector('#review-toast');
    if (!toast) return;
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(session.toastTimer);
    session.toastTimer = setTimeout(() => { toast.hidden = true; }, 2800);
  }

  function setWriteStatus(message, isError = false) {
    const status = session?.root.querySelector('#review-write-status');
    if (!status) return;
    status.textContent = message;
    status.classList.toggle('is-error', isError);
    session.writeMessage = message;
  }

  function wireMedia(playAfterLoad = false) {
    const active = session;
    const media = active.root.querySelector('[data-review-media]');
    if (!media) return;
    const signal = active.mediaController.signal;
    const key = active.selectedKey;
    const asset = selectedAsset();
    const preview = Number(asset?.preview_seconds);
    const restore = () => {
      if (session !== active || signal.aborted || !media.isConnected) return;
      const duration = Number.isFinite(media.duration) ? media.duration : Infinity;
      const max = Number.isFinite(preview) && preview > 0 ? Math.min(preview, duration) : duration;
      media.currentTime = Math.min(active.positions.get(key) || 0, Math.max(0, max - 0.05));
      if (playAfterLoad) media.play().catch(() => {
        if (session === active) showToast('素材已切換，請按播放鍵繼續。');
      });
    };
    if (media.readyState >= 1) restore();
    else media.addEventListener('loadedmetadata', restore, { once: true, signal });
    media.addEventListener('timeupdate', () => {
      if (session !== active || key !== active.selectedKey) return;
      if (Number.isFinite(preview) && preview > 0 && media.currentTime >= preview) {
        media.currentTime = preview;
        media.pause();
      }
      active.positions.set(key, media.currentTime);
      const clock = active.root.querySelector('#review-clock');
      if (clock) {
        const duration = finiteValue(asset?.duration_seconds);
        clock.textContent = `${formatTime(media.currentTime)}${duration !== null ? ` / ${formatTime(duration)}` : ''}`;
      }
    }, { signal });
    media.addEventListener('play', () => {
      if (session !== active) return media.pause();
      active.root.querySelectorAll('video,audio').forEach(other => { if (other !== media) other.pause(); });
    }, { signal });
  }

  function switchAsset(asset, preserveDraft = true) {
    if (!asset) return;
    const media = session.root.querySelector('[data-review-media]');
    const audioSwitch = selectedAsset()?.kind === 'audio' && asset.kind === 'audio';
    const resume = audioSwitch && media && !media.paused;
    const position = audioSwitch && media ? media.currentTime : null;
    rememberPlayback();
    if (position !== null) session.positions.set(assetKey(asset), position);
    session.kind = asset.kind;
    session.selectedKey = assetKey(asset);
    session.historicalAsset = null;
    if (!preserveDraft && !session.draft.trim()) session.draftContext = null;
    draw(resume);
  }

  function seek(seconds) {
    const media = session.root.querySelector('[data-review-media]');
    if (!media) return;
    const asset = selectedAsset();
    const preview = Number(asset?.preview_seconds);
    const duration = Number.isFinite(media.duration) ? media.duration : finiteValue(asset?.duration_seconds);
    let target = Math.max(0, Number(seconds) || 0);
    if (Number.isFinite(preview) && preview > 0) target = Math.min(target, preview);
    if (duration !== null && Number.isFinite(duration)) target = Math.min(target, duration);
    media.currentTime = target;
    session.positions.set(session.selectedKey, target);
  }

  async function saveComment() {
    const active = session;
    if (active.needsRebind) return setWriteStatus('請先確認並改綁目前素材，草稿仍保留。', true);
    const textarea = active.root.querySelector('#review-draft');
    active.draft = textarea?.value || active.draft;
    if (!active.draft.trim()) return showToast('先寫下一點回饋吧。');
    pinDraft(true);
    const context = active.draftContext;
    if (!context?.assetId || !context.assetSha256) return setWriteStatus('這個素材目前無法留言。', true);
    const button = active.root.querySelector('[data-action="save-comment"]');
    button.disabled = true;
    setWriteStatus('儲存中…');
    try {
      const payload = await requestJSON(apiPath(active.source, active.project, '/comments'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Haru-Review-CSRF': active.data.csrf_token },
        body: JSON.stringify({
          client_id: active.clientId,
          package_id: active.data.package_id,
          asset_id: context.assetId,
          asset_sha256: context.assetSha256,
          timestamp_seconds: context.timestamp,
          body: active.draft.trim(),
        }),
      });
      if (session !== active) return;
      const index = active.data.comments.findIndex(comment => String(comment.id) === String(payload.comment.id)
        || comment.client_id === payload.comment.client_id);
      if (index >= 0) active.data.comments[index] = payload.comment;
      else active.data.comments.push(payload.comment);
      active.draft = '';
      active.draftContext = null;
      active.clientId = newClientID();
      active.stale = false;
      active.needsRebind = false;
      active.writeMessage = '';
      draftCache.delete(projectKey(active.source, active.project));
      updateComments();
      showToast('留言已儲存。');
    } catch (error) {
      if (session !== active) return;
      if (error.status === 409 || error.status === 403) active.stale = true;
      const message = error.status === 409
        ? '版本已變更。草稿已保留，請確認版本後再重試。'
        : error.status === 403
          ? '寫入憑證已更新。草稿已保留，請重新載入版本。'
          : `儲存失敗：${error.message}。草稿已保留。`;
      setWriteStatus(message, true);
      cacheDraft(active);
      if (error.status === 409 || error.status === 403) updateComments();
    } finally {
      const currentButton = session === active ? active.root.querySelector('[data-action="save-comment"]') : null;
      if (currentButton) currentButton.disabled = active.needsRebind;
    }
  }

  async function toggleComment(id, status) {
    const active = session;
    const button = active.root.querySelector(`[data-resolve-id="${CSS.escape(String(id))}"]`);
    if (button) button.disabled = true;
    try {
      const payload = await requestJSON(apiPath(active.source, active.project, `/comments/${encodeURIComponent(id)}`), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', 'X-Haru-Review-CSRF': active.data.csrf_token },
        body: JSON.stringify({ status }),
      });
      if (session !== active) return;
      const index = active.data.comments.findIndex(comment => String(comment.id) === String(id));
      if (index >= 0) active.data.comments[index] = { ...active.data.comments[index], ...payload.comment };
      updateComments();
    } catch (error) {
      if (session !== active) return;
      if (button) button.disabled = false;
      if (error.status === 403) {
        active.stale = true;
        active.writeMessage = '寫入憑證已更新，請重新載入版本後再試。';
        updateComments();
      } else showToast(`更新失敗：${error.message}`);
    }
  }

  async function refreshReview() {
    const active = session;
    setWriteStatus('重新載入版本資料…');
    try {
      const data = await requestJSON(apiPath(active.source, active.project));
      if (session !== active) return;
      const assets = Array.isArray(data?.assets) ? data.assets.filter(asset => KIND_LABELS[asset.kind]) : [];
      if (!data?.project || !assets.length) throw new Error('目前沒有可審閱的素材');
      data.assets = assets;
      data.comments = Array.isArray(data.comments) ? data.comments : [];
      data.changes = Array.isArray(data.changes) ? data.changes : [];
      data.chapters = Array.isArray(data.chapters) ? data.chapters : [];
      data.warnings = Array.isArray(data.warnings) ? data.warnings : [];
      const alreadySaved = data.comments.find(comment => comment.client_id === active.clientId);
      active.data = data;
      active.historicalAsset = null;
      if (alreadySaved) {
        active.draft = '';
        active.draftContext = null;
        active.clientId = newClientID();
        active.needsRebind = false;
        active.stale = false;
        active.writeMessage = '留言已由伺服器儲存。';
        draftCache.delete(projectKey(active.source, active.project));
        draw();
        return;
      }
      const exact = active.draftContext && findAsset(active.draftContext.assetId, active.draftContext.assetSha256);
      active.needsRebind = Boolean(active.draftContext && !exact);
      active.stale = Boolean(active.draftContext);
      active.clientId = newClientID();
      active.writeMessage = !active.draftContext
        ? '版本資料已更新。'
        : active.needsRebind
        ? '原草稿的素材版本已不存在。請選好目前素材，再明確改綁。'
        : '版本資料已更新。草稿仍綁定原素材，可以重試。';
      const visible = findAsset(selectedAsset()?.id, selectedAsset()?.sha256) || currentAsset(active.kind) || active.data.assets[0];
      active.kind = visible.kind;
      active.selectedKey = assetKey(visible);
      cacheDraft(active);
      draw();
    } catch (error) {
      if (session !== active) return;
      setWriteStatus(`重新載入失敗：${error.message}。草稿已保留。`, true);
      cacheDraft(active);
    }
  }

  function rebindDraft() {
    const asset = selectedAsset();
    if (!asset) return;
    stopMedia();
    session.draftContext = snapshotContext(asset, asset.kind === 'cover' ? null : mediaTime());
    session.clientId = newClientID();
    session.needsRebind = false;
    session.writeMessage = '草稿已改綁目前素材。請確認後再送出。';
    updateComments();
  }

  function openComment(comment) {
    const snapshot = comment.asset || {};
    if (comment.asset_available === false || !safeMediaURL(snapshot.url)) {
      showToast('這則留言的舊版檔案已不可用。');
      return;
    }
    const exact = findAsset(snapshot.id, snapshot.sha256);
    const asset = exact || { ...snapshot, kind: snapshot.kind || 'video', role: 'historical' };
    const media = session.root.querySelector('[data-review-media]');
    const audioSwitch = selectedAsset()?.kind === 'audio' && asset.kind === 'audio';
    const resume = audioSwitch && media && !media.paused;
    const position = audioSwitch && media ? media.currentTime : null;
    rememberPlayback();
    if (position !== null) session.positions.set(assetKey(asset), position);
    session.kind = asset.kind;
    session.selectedKey = assetKey(asset);
    session.historicalAsset = exact ? null : asset;
    session.positions.set(session.selectedKey, Number(comment.timestamp_seconds) || 0);
    draw();
  }

  function handleClick(event) {
    const control = event.target.closest('button,[data-action]');
    if (!control || !session?.root.contains(control)) return;
    if (control.dataset.kind) {
      const asset = currentAsset(control.dataset.kind);
      if (asset) switchAsset(asset, true);
      return;
    }
    if (control.dataset.versionKey) {
      const asset = session.data.assets.find(item => assetKey(item) === control.dataset.versionKey);
      const media = session.root.querySelector('[data-review-media]');
      const wasPlaying = Boolean(media && !media.paused);
      const time = mediaTime();
      rememberPlayback();
      session.positions.set(assetKey(asset), time);
      session.selectedKey = assetKey(asset);
      session.historicalAsset = null;
      draw(wasPlaying);
      return;
    }
    if (control.dataset.changeAsset) {
      const asset = session.data.assets.find(item => assetKey(item) === control.dataset.changeAsset);
      switchAsset(asset, true);
      return;
    }
    if (control.dataset.seek !== undefined) return seek(control.dataset.seek);
    if (control.dataset.commentId) {
      const comment = session.data.comments.find(item => String(item.id) === control.dataset.commentId);
      if (comment) openComment(comment);
      return;
    }
    if (control.dataset.resolveId) return toggleComment(control.dataset.resolveId, control.dataset.resolveStatus);
    switch (control.dataset.action) {
      case 'back5': seek(mediaTime() - 5); break;
      case 'forward5': seek(mediaTime() + 5); break;
      case 'comment-now':
        pinDraft(true);
        session.root.querySelector('#review-draft')?.focus();
        break;
      case 'filter': session.onlyOpen = !session.onlyOpen; updateComments(); break;
      case 'save-comment': saveComment(); break;
      case 'refresh-review': refreshReview(); break;
      case 'rebind-draft': rebindDraft(); break;
      case 'show-files': {
        const showFiles = session.options.showFiles;
        unmount();
        showFiles?.();
        break;
      }
    }
  }

  function handleInput(event) {
    if (event.target.id !== 'review-draft') return;
    session.draft = event.target.value;
    if (session.draft && !session.draftContext) pinDraft(false);
  }

  function handleKeydown(event) {
    if (!session || event.target.closest('input,textarea,select,[contenteditable],video,audio')) return;
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    const media = session.root.querySelector('[data-review-media]');
    if (!media) return;
    event.preventDefault();
    seek(mediaTime() + (event.key === 'ArrowLeft' ? -5 : 5));
  }

  async function mount(container, source, project, options = {}) {
    unmount();
    if (!container || !source || !project) return false;
    const generation = ++mountGeneration;
    const controller = new AbortController();
    pendingController = controller;
    pendingContainer = container;
    container.setAttribute('aria-busy', 'true');
    let data;
    try {
      data = await requestJSON(apiPath(source, project), { signal: controller.signal });
    } catch (_) {
      if (generation === mountGeneration) container.removeAttribute('aria-busy');
      return false;
    }
    if (generation !== mountGeneration) return false;
    pendingController = null;
    pendingContainer = null;
    const assets = Array.isArray(data?.assets) ? data.assets.filter(asset => KIND_LABELS[asset.kind]) : [];
    if (!data?.project || !assets.length) {
      container.removeAttribute('aria-busy');
      return false;
    }
    data.assets = assets;
    data.comments = Array.isArray(data.comments) ? data.comments : [];
    data.changes = Array.isArray(data.changes) ? data.changes : [];
    data.chapters = Array.isArray(data.chapters) ? data.chapters : [];
    data.warnings = Array.isArray(data.warnings) ? data.warnings : [];
    const kind = ['video', 'audio', 'cover'].find(item => assets.some(asset => asset.kind === item));
    const first = assets.find(asset => asset.kind === kind && asset.role === 'current') || assets.find(asset => asset.kind === kind);
    const cacheKey = projectKey(source, project);
    const savedDraft = draftCache.get(cacheKey);
    if (savedDraft && data.comments.some(comment => comment.client_id === savedDraft.clientId)) {
      draftCache.delete(cacheKey);
    }
    const restored = draftCache.get(cacheKey);
    if (restored) {
      const exact = assets.some(asset => asset.id === restored.draftContext?.assetId
        && asset.sha256 === restored.draftContext?.assetSha256);
      if (restored.packageId !== data.package_id) {
        restored.stale = true;
        restored.needsRebind = Boolean(restored.draftContext && !exact);
        restored.clientId = newClientID();
        restored.writeMessage = restored.needsRebind
          ? '草稿原本綁定的素材已變更，請選擇目前素材後明確改綁。'
          : '專案版本已更新；草稿仍綁定同一素材，請確認後再送出。';
      }
    }
    session = {
      root: container, source, project, options, data, controller, kind,
      selectedKey: assetKey(first), historicalAsset: null, positions: new Map(),
      onlyOpen: true, draft: restored?.draft || '', draftContext: restored?.draftContext || null,
      clientId: restored?.clientId || newClientID(), toastTimer: null,
      mediaController: new AbortController(),
      stale: restored?.stale || false, needsRebind: restored?.needsRebind || false,
      writeMessage: restored?.writeMessage || '',
    };
    container.addEventListener('click', handleClick);
    container.addEventListener('input', handleInput);
    document.addEventListener('keydown', handleKeydown);
    container.removeAttribute('aria-busy');
    draw();
    return true;
  }

  function unmount() {
    mountGeneration += 1;
    pendingController?.abort();
    pendingContainer?.removeAttribute('aria-busy');
    pendingController = null;
    pendingContainer = null;
    if (!session) return;
    cacheDraft(session);
    stopMedia(session.root);
    session.mediaController.abort();
    session.controller.abort();
    clearTimeout(session.toastTimer);
    session.root.removeEventListener('click', handleClick);
    session.root.removeEventListener('input', handleInput);
    document.removeEventListener('keydown', handleKeydown);
    session = null;
  }

  window.HaruReview = { mount, unmount };
})();
