/* Project overview: explicit ownership, grouped shelves, and stable browsing state. */
(() => {
  'use strict';
  const TYPES = { video: '影片', library: '素材庫', experiment: '實驗室', unclassified: '待整理' };
  const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const route = project => `#/project/${encodeURIComponent(project.source)}/${encodeURIComponent(project.name)}`;
  const savedCategory = new URL(location.href).searchParams.get('category');
  let category = Object.hasOwn(TYPES, savedCategory) ? savedCategory : 'video';
  let root, data, options, lastRender = '';
  const expanded = new Set();
  const scrollPositions = new Map();

  function primaryLink(group, body, className = '') {
    return group.primary
      ? `<a class="${className}" href="${route(group.primary)}">${body}</a>`
      : `<div class="${className}">${body}</div>`;
  }

  function status(group) {
    if (!group.primary) return '<span class="overview-status missing">主專案目前不可用</span>';
    if (group.primary.upload?.state === 'uploaded') return '<span class="overview-status uploaded">已有上傳紀錄</span>';
    if (group.primary.lease?.active) return '<span class="overview-status running">正在執行</span>';
    return '<span class="overview-status">進度見專案頁</span>';
  }

  function cover(group) {
    const url = typeof group.cover === 'string' && /^\/media\/(studio|media)\//.test(group.cover) ? group.cover : null;
    const content = url
      ? `<img src="${escapeHTML(url)}" alt="${escapeHTML(group.title)}封面" loading="lazy">`
      : '<span class="overview-no-cover">尚未指定封面</span>';
    return primaryLink(group, content, 'overview-cover');
  }

  function memberList(group) {
    if (!expanded.has(group.id)) return '';
    return `<div class="overview-members">${group.members.map(member => `<a href="${route(member)}"><span>${escapeHTML(member.label || member.name)}</span><small>${escapeHTML(member.source)} / ${escapeHTML(member.name)}</small><span aria-hidden="true">↗</span></a>`).join('')}${group.reason ? `<p>${escapeHTML(group.reason)}</p>` : ''}</div>`;
  }

  function card(group) {
    const isOpen = expanded.has(group.id);
    return `<article class="overview-card" data-group-id="${escapeHTML(group.id)}">${cover(group)}<div class="overview-card-copy">${status(group)}<h3>${primaryLink(group, escapeHTML(group.title))}</h3><div class="overview-card-foot"><button type="button" data-overview-action="members" data-group-id="${escapeHTML(group.id)}" data-focus-key="members:${escapeHTML(group.id)}" aria-label="${escapeHTML(group.title)}：${group.members.length} 個版本與來源" aria-expanded="${isOpen}">${group.members.length > 1 ? `${group.members.length} 個版本／來源` : '查看來源'} <span aria-hidden="true">${isOpen ? '−' : '＋'}</span></button>${group.primary ? `<a class="overview-open" href="${route(group.primary)}">${group.type === 'video' ? '開啟審片' : '查看內容'} <span aria-hidden="true">↗</span></a>` : '<span class="overview-open unavailable">展開查看可用版本</span>'}</div></div>${memberList(group)}</article>`;
  }

  function matches(group) {
    const query = String(options.query || '').trim().toLocaleLowerCase();
    const haystack = [group.title, group.collection, ...(group.keywords || []), ...group.members.flatMap(member => [member.name, member.label])].join(' ').toLocaleLowerCase();
    if (query && !haystack.includes(query)) return false;
    // Source filters include any source; lifecycle filters describe the selected primary.
    if (options.filter === 'studio' || options.filter === 'media') return group.members.some(member => member.source === options.filter);
    if (!options.match || options.filter === 'all') return true;
    return Boolean(group.primary && options.match(group.primary));
  }

  function rememberScroll() {
    if (!root) return;
    root.querySelectorAll('[data-overview-shelf]').forEach(shelf => scrollPositions.set(`${category}:${shelf.dataset.overviewShelf}`, shelf.scrollLeft));
  }

  function draw() {
    if (!root || !data) return;
    const signature = JSON.stringify([data, category, options.query, options.filter, [...expanded]]);
    if (signature === lastRender) return;
    lastRender = signature;
    rememberScroll();
    const focus = root.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
    const groups = [...data.groups].sort((a, b) => String(b.primary?.updated_at || '').localeCompare(String(a.primary?.updated_at || ''))).filter(group => group.type === category && matches(group));
    const shelves = new Map();
    groups.forEach(group => {
      const name = group.collection || TYPES[category];
      if (!shelves.has(name)) shelves.set(name, []);
      shelves.get(name).push(group);
    });
    root.className = 'studio-overview';
    root.innerHTML = `<div class="overview-heading"><div><p class="overview-eyebrow">HARU STUDIO / 作品整理</p><h1>${TYPES[category]}</h1><p class="overview-subtitle">一個企劃，一個入口。試片與歷史版本收在各片底下。</p></div><div class="overview-count"><strong>${Number(data.counts.video) || 0}</strong><span>支影片企劃</span><small>從 ${Number(data.source_count) || 0} 個資料夾整理</small></div></div><nav class="overview-tabs" aria-label="內容分類">${Object.entries(TYPES).map(([type,label]) => `<button type="button" data-overview-action="category" data-category="${type}" data-focus-key="category:${type}" aria-pressed="${category === type}">${label}<span>${Number(data.counts[type]) || 0}</span></button>`).join('')}</nav><div class="overview-result-line" role="status" aria-live="polite"><span>${groups.length} 個${category === 'video' ? '企劃' : '項目'}${options.query ? ` · 搜尋「${escapeHTML(options.query)}」` : ''}</span><span>版本可展開 · 按主題瀏覽</span></div>${groups.length ? `<div class="overview-shelves">${[...shelves].map(([name, entries], index) => `<section class="overview-shelf" aria-labelledby="overview-shelf-title-${index}"><div class="overview-shelf-heading"><h2 id="overview-shelf-title-${index}">${escapeHTML(name)}</h2><span>${entries.length} 個${category === 'video' ? '企劃' : '項目'}</span>${entries.length > 1 ? `<div class="overview-shelf-controls"><button type="button" data-overview-action="scroll" data-shelf="${escapeHTML(name)}" data-direction="-1" data-focus-key="prev:${escapeHTML(name)}" aria-label="${escapeHTML(name)}：向左瀏覽">←</button><button type="button" data-overview-action="scroll" data-shelf="${escapeHTML(name)}" data-direction="1" data-focus-key="next:${escapeHTML(name)}" aria-label="${escapeHTML(name)}：向右瀏覽">→</button></div>` : ''}</div><div class="overview-shelf-items" data-overview-shelf="${escapeHTML(name)}" tabindex="0" role="region" aria-label="${escapeHTML(name)}書架">${entries.map(card).join('')}</div></section>`).join('')}</div>` : `<div class="overview-empty"><p>${category === 'video' && !data.counts.video ? '還沒有指定影片企劃。' : '這個分類沒有符合的項目。'}</p><p>可清除搜尋、切換上方篩選，或到「待整理」查看尚未分類的內容。</p></div>`}`;
    root.querySelectorAll('[data-overview-shelf]').forEach(shelf => {
      shelf.scrollLeft = scrollPositions.get(`${category}:${shelf.dataset.overviewShelf}`) || 0;
    });
    if (focus) [...root.querySelectorAll('[data-focus-key]')].find(element => element.dataset.focusKey === focus)?.focus({preventScroll:true});
  }

  function onClick(event) {
    const control = event.target.closest('[data-overview-action]');
    if (!control || !root.contains(control)) return;
    switch (control.dataset.overviewAction) {
      case 'category': {
        rememberScroll();
        category = control.dataset.category;
        const url = new URL(location.href);
        url.searchParams.set('category', category);
        history.replaceState(null, '', url);
        // Old category's shelves must not overwrite the new category's stored positions.
        root.querySelectorAll('[data-overview-shelf]').forEach(shelf => shelf.removeAttribute('data-overview-shelf'));
        draw();
        break;
      }
      case 'members':
        expanded.has(control.dataset.groupId) ? expanded.delete(control.dataset.groupId) : expanded.add(control.dataset.groupId);
        draw();
        break;
      case 'scroll': {
        const shelf = [...root.querySelectorAll('[data-overview-shelf]')].find(element => element.dataset.overviewShelf === control.dataset.shelf);
        if (!shelf) return;
        shelf.scrollBy({left: Number(control.dataset.direction) * Math.max(300, shelf.clientWidth * 0.8), behavior:matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
        break;
      }
    }
  }

  function onImageError(event) {
    if (event.target.tagName !== 'IMG') return;
    const placeholder = document.createElement('span');
    placeholder.className = 'overview-no-cover';
    placeholder.textContent = '封面暫時無法載入';
    event.target.replaceWith(placeholder);
  }

  function render(container, payload, renderOptions) {
    if (root !== container) {
      root?.removeEventListener('click', onClick);
      root?.removeEventListener('error', onImageError, true);
      root = container;
      root.addEventListener('click', onClick);
      root.addEventListener('error', onImageError, true);
      lastRender = '';
    }
    data = payload;
    options = renderOptions;
    draw();
  }

  window.HaruOverview = { render };
})();
