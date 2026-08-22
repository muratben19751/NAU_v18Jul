// cloudflared'i TCP'de karsilar (node TCP send serbest), istekleri nau-web'in
// ISIMLI BORUSUNA gecirir (python boru yazimi Kaspersky TCP filtresini atlar).
const http = require('http');
const B = String.fromCharCode(92);
const PIPE = B+B+'.'+B+'pipe'+B+'nauweb';   // \.\pipe\nauweb
const PORT = parseInt(process.argv[2] || '8111', 10);
const HOST = '127.0.0.1';
http.createServer((creq, cres) => {
  const opts = { socketPath: PIPE, path: creq.url, method: creq.method, headers: creq.headers };
  const preq = http.request(opts, pres => { cres.writeHead(pres.statusCode, pres.headers); pres.pipe(cres); });
  preq.on('error', e => { try { cres.writeHead(502, {'content-type':'text/plain'}); } catch(_){} cres.end('node_proxy: '+e.message); });
  creq.pipe(preq);
}).listen(PORT, HOST, () => console.error('node_proxy '+HOST+':'+PORT+' -> pipe'));
