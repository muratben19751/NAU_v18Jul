// PM2 girişi — `pm2 start ecosystem.config.js` (bkz. serve.py docstring'i:
// uygulama yalnız 127.0.0.1'e bağlanır, dışarıya Cloudflare tüneli açar).
// Desen sistem1-v4/ecosystem.config.js ile aynı: tam interpreter yolu +
// interpreter:"none" (pm2'nin .js sanıp node ile açmasını engeller).
// python.exe bilerek (pythonw değil): serve.py'nin açılış uyarıları
// (EXTERNAL_CATALOGS, NAUTILUS_INDEX_ROOT…) pm2 loguna düşsün.
module.exports = {
  apps: [
    {
      name: "nau-web",
      script: "C:\\Users\\MYDESK\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      args: "serve.py --port 8111",
      cwd: "C:\\myAI_Projects\\NAU_v18Jul",
      interpreter: "none",
      autorestart: true,
      env: {
        PYTHONUNBUFFERED: "1",
        // Yerel LLM ucu (llama-server, Qwen3.8-27B). Varsayılan backend
        // DEĞİŞMİYOR — Claude yolu olduğu gibi duruyor; bu üçlü yalnız model
        // seçicisine "OR · qwen3.8-27b" satırını ekler ve o satır seçilince
        // koşu localhost'a pinlenir (set_thread_model → "or:<id>").
        //
        // DİKKAT: OPENROUTER_BASE_URL localhost'a bakarken GERÇEK openrouter.ai
        // uçları erişilemez olur — ikisi aynı anda kullanılamaz.
        OPENROUTER_BASE_URL: "http://127.0.0.1:8080/v1",
        OPENROUTER_API_KEY: "local", // llama-server anahtar doğrulamıyor, boş olamıyor
        // Pin listenin YERİNE geçer: ağa çıkmaz, ücretsiz filtresinden muaftır.
        NAUTILUS_OPENROUTER_MODELS: "qwen3.8-27b",
        // custom_block yolunun kendi deadline'ı; varsayılan 75 s, izin verilen
        // tavan 120 s (agent.py `_call_claude_for_block`). Ölçüldü 2026-08-15:
        // yerel Qwen3.8-27B'de custom_block başarısı 75 s'de 2/8, 120 s'de 4/8.
        // Sebep AGENT_CUSTOM_BLOCK_MAX_TOKENS'ın 1800'lük SERT tavanı (hi=1_800):
        // düşünen model onu her çağrıda aşıyor, kesilme 7200'e retry doğuruyor,
        // retry de deadline'ı yiyor. Claude'a etkisi yok — o 75 s'in çok altında
        // bitiriyor; bu yalnız yavaş sağlayıcıda hata tespitini geciktirir.
        AGENT_CUSTOM_BLOCK_TIMEOUT: "120",
        // HİBRİT: koşu yerel uca pinlense bile `custom_block` Claude'da kalır.
        // Ölçüm 2026-08-15 — yerel Qwen3.8-27B: composed 10/10, custom_block
        // 4/8. Sebep o yolun terse bir modele göre kalibre edilmiş 1800'lük
        // SERT tavanı; blok kodu ayrıca codegate'in AST + rol sözleşmesinden
        // geçmek zorunda. Hacim işi ucuz uçta, sözleşmesi katı iş güvenilir uçta.
        //
        // Dört yolun DÖRDÜ de ölçüldü (2026-08-15, yerel Qwen3.8-27B):
        //   narrative    6/6   3.2-11.3 s   → yerelde
        //   idea         8/8   28.7-189 s   → yerelde
        //   composed    10/10  27-222 s     → yerelde
        //   custom_block 4/8   20-226 s     → CLAUDE'DA (aşağıdaki eşleme)
        // Desen: çıktının sözleşmesi katılaştıkça (düz metin → JSON → codegate'ten
        // geçen Python) yerel modelin başarısı düşüyor. Bu liste ölçümdür, tahmin
        // değil; değiştirmeden önce yeniden ölçün.
        NAUTILUS_MODEL_BY_PURPOSE: "custom_block=claude-fable-5",
        // Genel LLM çağrı deadline'ı (varsayılan 120 s). Ölçüm 2026-08-15:
        // yerel `idea` üretimleri 28.7-189.2 s arasında sürdü ve en uzunu TEK
        // çağrıydı (9.932 çıktı token'ı) — 120 s ile 8 üretimin 1'i (~%12)
        // timeout'a düşerdi. O üretim kesilmedi, geçerli cevap verdi; eksik olan
        // tek şey beklemekti.
        //
        // Sebep bağlı-sabit: öğrenilen max_tokens tavanı (1500 → 16000) kesilmeyi
        // çözerken modelin ~10k token yazmasına izin veriyor; ~52 tok/s'de bu
        // ~190 s eder. Bir sabiti kalibre ederken ona bağlı olanı da kalibre et.
        //
        // Bedeli GLOBAL: Claude çağrılarında da bir arıza artık 120 s yerine
        // 300 s'de fark edilir. Claude bu promptları 30 s'nin altında bitirdiği
        // için normal koşuda hiçbir şey değişmez — fark yalnız arıza anında.
        NAUTILUS_LLM_CALL_TIMEOUT: "300",
        // AUTO koşusunun PARA tavanı (varsayılan 5). Ölçüm 2026-08-16, koşu
        // 5e89d42a: 5 USD 66 dakika ve 2 tam tur aldı — turun sonuna kadar
        // gidemeden kesildi, mühürlü holdout'a hiç ulaşılamadı.
        //
        // 20 USD ≈ 4,4 saat demek, yani bağlayıcı tavan artık PARA değil SÜRE
        // (max_hours=4) olur. Kasıtlı: koşu ayrılan süreyi kullanır, para da
        // arkada emniyet olarak durur. Faturanın tamamını `custom_block`
        // (Claude) yolu harcıyor — yerel modelin çağrıları 0 USD.
        //
        // HARD_MAX_COST_USD bunu varsayılan olarak izler, yani sert tavan da
        // 20'ye çıkar. Düşürmek için AGENT_HARD_MAX_COST_USD'yi ayrıca ver.
        AGENT_DEFAULT_MAX_COST_USD: "20",
      },
    },
  ],
};
