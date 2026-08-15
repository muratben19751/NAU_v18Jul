// PM2 girişi — `pm2 start twitter/ecosystem.config.js`
//
// Depo kökündeki ecosystem.config.js'ten AYRI, bilerek: bu klasör Nautilus
// uygulamasının parçası değil. İzleyici çökse `nau-web` etkilenmez, `nau-web`
// yeniden başlatılsa izleme kesilmez.
//
// SIR YOK: bu dosya git'te. XWATCH_SMTP_PASSWORD (ve isteğe bağlı
// XWATCH_X_PASSWORD) Windows ortamına konur, pm2 miras alır:
//   setx XWATCH_SMTP_PASSWORD "<gmail uygulama şifresi>"
// ardından YENİ bir terminalden `pm2 restart x-watch --update-env`.
module.exports = {
  apps: [
    {
      name: "x-watch",
      script: "C:\\Users\\MYDESK\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      args: "x_watch.py",
      cwd: "C:\\myAI_Projects\\NAU_v18Jul\\twitter",
      interpreter: "none", // pm2 .js sanıp node ile açmasın
      autorestart: true,
      // Playwright'ı her çökmede yeniden başlatmak ucuz değil; art arda hızlı
      // ölüyorsa (ör. oturum düşmüş, exit 2) pm2 geri çekilsin.
      restart_delay: 30000,
      max_restarts: 20,
      env: {
        PYTHONUNBUFFERED: "1", // açılış satırları pm2 loguna düşsün
        XWATCH_QUERY: "ttkom",
        XWATCH_INTERVAL_S: "300",
        // Sır değil; tek yerden görünür olması iyi.
        XWATCH_MAIL_TO: "muratben@gmail.com",
        XWATCH_SMTP_USER: "muratben@gmail.com",
      },
    },
  ],
};
