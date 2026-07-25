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

// Validation errors (422) -> banner
document.body.addEventListener('htmx:responseError', (e) => {
  const banner = document.getElementById('error-banner');
  banner.textContent = e.detail.xhr.responseText || 'Request failed';
  banner.hidden = false;
  clearTimeout(banner._t);
  banner._t = setTimeout(() => { banner.hidden = true; }, 4000);
});
