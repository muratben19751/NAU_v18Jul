// Phase 2: tabs + inline chip editing + error banner.

// Tab switching — remembers the active tab across OOB side-panel refreshes
let activeTab = 'opt';
function applyTab(name) {
  const panel = document.getElementById('side-panel');
  if (!panel) return;
  panel.querySelectorAll('.tab').forEach(x =>
    x.classList.toggle('active', x.dataset.tab === name));
  panel.querySelectorAll('.pane').forEach(x =>
    x.classList.toggle('active', x.id === 'pane-' + name));
}
document.addEventListener('click', (e) => {
  const t = e.target.closest('.tab');
  if (!t) return;
  activeTab = t.dataset.tab;
  applyTab(activeTab);
});
// htmx replaces #side-panel (targeted or OOB) -> re-apply the remembered tab
document.body.addEventListener('htmx:afterSwap', () => applyTab(activeTab));
document.body.addEventListener('htmx:oobAfterSwap', () => applyTab(activeTab));

// Inline chip editing: click a .pchip.editable -> input; Enter/blur commits
document.addEventListener('click', (e) => {
  const chip = e.target.closest('.pchip.editable');
  if (!chip || chip.querySelector('input')) return;
  const block = chip.closest('.block');
  const input = document.createElement('input');
  input.value = chip.dataset.value;
  input.name = 'value';
  input.setAttribute('hx-patch', chip.dataset.url);
  input.setAttribute('hx-target', '#' + block.id);
  input.setAttribute('hx-swap', 'outerHTML');
  input.setAttribute('hx-trigger', "keyup[key=='Enter'], blur changed");
  // rule edits send `param`, risk edits send `name`
  const key = chip.dataset.url.endsWith('/risk') ? 'name' : 'param';
  input.setAttribute('hx-vals', JSON.stringify({ [key]: chip.dataset.param }));
  chip.textContent = '';
  chip.appendChild(input);
  htmx.process(chip);
  input.focus();
  input.select();
});

// ── Indicator library: search, click-to-add, drag-and-drop ──────────────
// Rules are added through the per-block <form class="add-rule-form"> already in
// the DOM, so the library never needs its own endpoint or CSRF surface: it just
// fills that form's <select> and submits it via htmx.

function libSearch(term) {
  const q = term.trim().toLowerCase();
  document.querySelectorAll('.library .lib-item').forEach(item => {
    const hit = !q || item.textContent.toLowerCase().includes(q);
    item.style.display = hit ? '' : 'none';
  });
  // Hide a category heading when the filter emptied it.
  document.querySelectorAll('.library .lib-title').forEach(title => {
    let el = title.nextElementSibling, any = false;
    while (el && el.classList.contains('lib-item')) {
      if (el.style.display !== 'none') { any = true; break; }
      el = el.nextElementSibling;
    }
    title.style.display = any ? '' : 'none';
  });
}

document.addEventListener('input', (e) => {
  if (e.target.classList.contains('lib-search')) libSearch(e.target.value);
});

// Submit the target block's existing add-rule form with the chosen indicator.
function addIndicator(zone, key) {
  const form = zone.querySelector('.add-rule-form');
  if (!form) return;
  const select = form.querySelector('select[name="indicator"]');
  // An indicator the block's own dropdown doesn't offer isn't addable here.
  if (!select || !select.querySelector(`option[value="${CSS.escape(key)}"]`)) return;
  select.value = key;
  htmx.trigger(form, 'submit');
}

document.addEventListener('dragstart', (e) => {
  const item = e.target.closest('.lib-item');
  if (!item) return;
  e.dataTransfer.setData('text/plain', item.dataset.key);
  e.dataTransfer.effectAllowed = 'copy';
  item.classList.add('dragging');
});
document.addEventListener('dragend', (e) => {
  const item = e.target.closest('.lib-item');
  if (item) item.classList.remove('dragging');
  document.querySelectorAll('.drop-hover')
    .forEach(z => z.classList.remove('drop-hover'));
});

document.addEventListener('dragover', (e) => {
  const zone = e.target.closest('[data-dropzone]');
  if (!zone) return;
  e.preventDefault();  // required for the drop event to fire at all
  e.dataTransfer.dropEffect = 'copy';
  zone.classList.add('drop-hover');
});
document.addEventListener('dragleave', (e) => {
  const zone = e.target.closest('[data-dropzone]');
  // Ignore leaves fired while moving between the zone's own children.
  if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove('drop-hover');
});
document.addEventListener('drop', (e) => {
  const zone = e.target.closest('[data-dropzone]');
  if (!zone) return;
  e.preventDefault();
  zone.classList.remove('drop-hover');
  const key = e.dataTransfer.getData('text/plain');
  if (key) addIndicator(zone, key);
});

// Click-to-add: drops the indicator into the entry block (the common case),
// so the library stays usable without dragging.
document.addEventListener('click', (e) => {
  const item = e.target.closest('.lib-item');
  if (!item) return;
  const zone = document.querySelector('[data-dropzone="entry"]');
  if (zone) addIndicator(zone, item.dataset.key);
});

// Validation errors (422) -> banner
document.body.addEventListener('htmx:responseError', (e) => {
  const banner = document.getElementById('error-banner');
  banner.textContent = e.detail.xhr.responseText || 'Request failed';
  banner.hidden = false;
  clearTimeout(banner._t);
  banner._t = setTimeout(() => { banner.hidden = true; }, 4000);
});
