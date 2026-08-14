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
      },
    },
  ],
};
