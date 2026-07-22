// Studio E2E — son kullanıcı perspektifiyle headed test.
// npx MCP yok; motor aynı (Playwright). Pencere görünür açılır.
import pw from '/Users/i034216/.npm/_npx/e41f203b7505f1fb/node_modules/playwright/index.js';
const { chromium } = pw;

const BASE = 'http://127.0.0.1:8000';
const OUT = process.env.OUT_DIR || '/tmp/studio_e2e';
import { mkdirSync } from 'fs';
mkdirSync(OUT, { recursive: true });

const report = { console_errors: [], page_errors: [], network: [], steps: [], findings: [] };
const log = (m) => { console.log(m); report.steps.push(m); };
const shot = async (page, name) => { const p = `${OUT}/${name}.png`; await page.screenshot({ path: p, fullPage: true }); return p; };

const browser = await chromium.launch({ headless: false, slowMo: 350 });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();

page.on('console', (msg) => {
  if (msg.type() === 'error') report.console_errors.push(msg.text());
});
page.on('pageerror', (err) => report.page_errors.push(String(err)));
page.on('request', (req) => {
  const u = req.url();
  if (u.includes('/strategy/') || u.includes('/agent/') || u.includes('/suggest'))
    report.network.push(`${req.method()} ${u.replace(BASE, '')}`);
});
page.on('response', (resp) => {
  if (resp.status() >= 400) report.network.push(`⚠️${resp.status()} ${resp.url().replace(BASE, '')}`);
});

try {
  // ===== 1. Başlangıç ve Düzen =====
  await page.goto(`${BASE}/studio`, { waitUntil: 'networkidle' });
  log(`1. /studio yüklendi — title="${await page.title()}"`);
  await shot(page, '01_initial');

  // sol nav
  const navTexts = await page.locator('.nav a, nav a, aside a').allInnerTexts().catch(() => []);
  log(`   sol nav linkleri: ${JSON.stringify(navTexts.slice(0, 12))}`);

  // preview panel sayısı (HATA-1 regresyon kontrolü)
  const previewCount = await page.locator('#strategy-preview').count();
  log(`   #strategy-preview DOM sayısı = ${previewCount} (beklenen: 1)`);
  if (previewCount !== 1) report.findings.push(`⚠️ #strategy-preview ${previewCount} adet (duplike id regresyonu)`);

  // OTONOM / MANUEL toggle
  const modeBtns = page.locator('.mode-btn');
  const modeCount = await modeBtns.count();
  log(`   mode-btn sayısı = ${modeCount}`);
  if (modeCount >= 2) {
    await modeBtns.nth(0).click(); await page.waitForTimeout(400);
    log(`   OTONOM tıklandı — otonom panel görünür? ${await page.locator('.lab-modepanel.active, #agent-result').first().isVisible().catch(()=>false)}`);
    await shot(page, '02_otonom');
    await modeBtns.nth(1).click(); await page.waitForTimeout(400);
    log(`   MANUEL tıklandı — compose görünür? ${await page.locator('#save-form').isVisible().catch(()=>false)}`);
    await shot(page, '03_manuel');
  }

  // ===== 2. Metadata & validasyon =====
  const name = page.locator('#name-input');
  const longName = 'My_Strategy_v1.0!@#$%^&*()_this_is_a_very_long_strategy_name_1234567890';
  await name.fill(longName);
  const nameOverflow = await name.evaluate((el) => el.scrollWidth > el.clientWidth + 2);
  log(`2. NAME uzun/özel karakter girildi — input taşıyor mu (scroll>client)? ${nameOverflow}`);

  const size = page.locator('#trade-size-usdt-input');
  await size.fill('-1000');
  const negVal = await size.inputValue();
  const negValid = await size.evaluate((el) => el.checkValidity());
  log(`   TRADE SIZE=-1000 → value="${negVal}" checkValidity=${negValid} (min=${await size.getAttribute('min')})`);
  if (negValid) report.findings.push('⚠️ trade_size negatif değeri client-side geçerli sayıyor (min var ama submit engellenmez)');

  await size.fill(''); await size.type('bin dolar');
  log(`   TRADE SIZE="bin dolar" → value="${await size.inputValue()}" (number input harf reddediyor mu?)`);

  await size.fill('999999999');
  const bigValid = await size.evaluate((el) => el.checkValidity());
  log(`   TRADE SIZE=999999999 → checkValidity=${bigValid} (max=${await size.getAttribute('max')})`);
  if (!bigValid) log('   → max sınırı üstünde, tarayıcı geçersiz sayıyor (iyi)');

  const desc = page.locator('#description-input');
  await desc.fill('satır1\nsatır2\nsatır3\nçok satırlı açıklama testi — uzun metin '.repeat(3));
  log(`   DESCRIPTION multiline dolduruldu`);
  await size.fill('1000'); await name.fill('QA Test Strategy');
  await shot(page, '04_metadata');

  // ===== 3. Sinyal bloğu & kantitatif =====
  const btype = page.locator('#add-block-form select[name="type"]');
  await btype.selectOption('ma_cross');
  await page.waitForTimeout(600); // block_form.html HTMX ile yüklenir
  log(`3. BLOCK TYPE=ma_cross seçildi`);

  // Fast > Slow mantık hatası
  const fast = page.locator('#block-params [name="p_fast"]');
  const slow = page.locator('#block-params [name="p_slow"]');
  if (await fast.count() && await slow.count()) {
    await fast.fill('50'); await slow.fill('20');
    log(`   Fast=50 Slow=20 (mantık hatası) girildi`);
    const resp1 = page.waitForResponse((r) => r.url().includes('/strategy/drafts') && r.request().method() === 'POST');
    await page.locator('#add-block-form button[type="submit"]').click();
    await resp1.then((r) => log(`   POST /strategy/drafts → ${r.status()}`)).catch(() => log('   POST /strategy/drafts yanıtı yakalanamadı'));
    await page.waitForTimeout(500);
    const draftsTxt = await page.locator('#drafts').innerText().catch(() => '');
    const errBanner = await page.locator('#drafts .empty-state, #drafts .badge').allInnerTexts().catch(() => []);
    const rowCount = await page.locator('#drafts table.data tbody tr').count();
    log(`   → Fast>Slow submit sonrası draft satırı=${rowCount}, drafts içeriği ilk 80ch="${draftsTxt.slice(0,80).replace(/\n/g,' ')}"`);
    if (rowCount > 0) report.findings.push('⚠️ Fast(50)>Slow(20) bloğu draft olarak EKLENDİ — mantıksal uyarı yok (validasyon save anında)');

    // periyot 0 / negatif
    await fast.fill('0'); await slow.fill('-5');
    const f0 = await fast.evaluate((el) => el.checkValidity());
    log(`   Fast=0 Slow=-5 → fast.checkValidity=${f0} (min=${await fast.getAttribute('min')})`);
  } else {
    log('   ⚠️ #block-params içinde fast/slow input bulunamadı');
    report.findings.push('⚠️ ma_cross seçilince fast/slow parametre inputları render olmadı');
  }

  // normal değerler + ekle
  await btype.selectOption('ma_cross'); await page.waitForTimeout(500);
  await page.locator('#block-params [name="p_fast"]').fill('10');
  await page.locator('#block-params [name="p_slow"]').fill('30');
  const dir = page.locator('#block-params [name="p_direction"]');
  if (await dir.count()) await dir.selectOption('up').catch(() => {});
  const resp2 = page.waitForResponse((r) => r.url().includes('/strategy/drafts') && r.request().method() === 'POST');
  await page.locator('#add-block-form button[type="submit"]').click();
  await resp2.then((r) => log(`   POST /strategy/drafts → ${r.status()}`)).catch(() => log('   yanıt yakalanamadı'));
  await page.waitForTimeout(600);
  const rc2 = await page.locator('#drafts table.data tbody tr').count();
  log(`   normal (10/30/up) eklendi → draft satırı=${rc2}`);
  log(`   [adım 3 network]: ${JSON.stringify(report.network)}`);
  await shot(page, '05_blocks');

  // ===== 4. UI state & preview =====
  const pv = await page.locator('#strategy-preview').innerText().catch(() => '');
  log(`4. STRATEGY PREVIEW ilk 120ch="${pv.slice(0,120).replace(/\n/g,' ')}"`);
  const pvBlocks = /ma_cross|MA Cross|entry/i.test(pv);
  log(`   preview blok/entry yansıtıyor mu? ${pvBlocks}`);
  if (!pvBlocks && rc2 > 0) report.findings.push('⚠️ Blok eklendi ama STRATEGY PREVIEW güncellenmedi (OOB swap başarısız?)');
  const previewCount2 = await page.locator('#strategy-preview').count();
  if (previewCount2 !== 1) report.findings.push(`⚠️ blok eklemeden sonra #strategy-preview ${previewCount2} adet`);

  // ===== 5. AI butonları =====
  report.network.length = 0;
  const suggestBtn = page.locator('button:has-text("Ask Claude to suggest")');
  if (await suggestBtn.count()) {
    await suggestBtn.click();
    const spinnerVisible = await page.locator('#suggest-spinner').isVisible().catch(() => false);
    log(`5. "Ask Claude to suggest" tıklandı — spinner göründü? ${spinnerVisible}`);
    await page.waitForTimeout(1500);
    log(`   suggest sonrası network: ${JSON.stringify(report.network)}`);
  }
  const editBtn = page.locator('button:has-text("💬 AI ile düzenle")').first();
  if (await editBtn.count()) {
    report.network.length = 0;
    await editBtn.click(); await page.waitForTimeout(1200);
    const chatOpen = await page.locator('#drafts-edit-chat').innerText().catch(() => '');
    log(`   "AI ile düzenle" tıklandı — chat açıldı? len=${chatOpen.length}, network=${JSON.stringify(report.network)}`);
  }
  await shot(page, '06_ai');

  // ===== 6. Custom Blocks şişme kontrolü (performans) =====
  const customBtnCount = await page.locator('button:has-text("✏️ AI ile düzenle")').count();
  const catalogRows = await page.locator('#catalog .catalog-row, #catalog table tbody tr, #catalog li').count().catch(() => 0);
  log(`6. custom-block "✏️ AI ile düzenle" buton sayısı = ${customBtnCount}`);
  if (customBtnCount > 100) report.findings.push(`⚠️ KRİTİK: 06·Custom Blocks listesi ${customBtnCount} blok render ediyor — sayfa ~1.3MB, DOM şişmesi (otonom agent'ın ürettiği geçici agnt_* blokları temizlenmiyor)`);

  log(`\n=== KONSOL HATALARI (${report.console_errors.length}) ===`);
  report.console_errors.forEach((e) => log(`   ❌ ${e}`));
  log(`=== PAGE ERRORS (${report.page_errors.length}) ===`);
  report.page_errors.forEach((e) => log(`   ❌ ${e}`));
} catch (err) {
  log(`\n💥 BETİK HATASI: ${err.message}`);
  report.findings.push(`💥 test harness hatası: ${err.message}`);
  await shot(page, 'ZZ_crash').catch(() => {});
} finally {
  const { writeFileSync } = await import('fs');
  writeFileSync(`${OUT}/report.json`, JSON.stringify(report, null, 2));
  log(`\n=== BULGULAR (${report.findings.length}) ===`);
  report.findings.forEach((f) => log(`   ${f}`));
  log(`\nrapor: ${OUT}/report.json | ekran görüntüleri: ${OUT}/*.png`);
  await page.waitForTimeout(1500);
  await browser.close();
}
