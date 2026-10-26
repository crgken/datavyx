// Run only against our own isolated probe and fake microphone fixture.
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const language = process.argv[2];
if (!['zh', 'en'].includes(language)) throw new Error('Expected zh or en');
const port = language === 'zh' ? 9225 : 9226;
const fixture = path.resolve(root, '../knowledge-management-demo/artifacts/speech-fixtures', `${language}.wav`);
const pause = ms => new Promise(r => setTimeout(r, ms));
class Client {
  constructor(url) {
    this.socket = new WebSocket(url);
    this.pending = new Map(); this.serial = 0;
    this.ready = new Promise(r => this.socket.addEventListener('open', r, {once: true}));
    this.socket.addEventListener('message', e => {
      const message = JSON.parse(e.data);
      if (message.id) {
        const row = this.pending.get(message.id);
        if (row) {clearTimeout(row.timer);this.pending.delete(message.id);row.resolve(message.result);}
      }
    });
  }
  async call(method, params = {}) {
    await this.ready;
    const id = ++this.serial;
    return new Promise((resolve,reject) => {
      const timer = setTimeout(() => {this.pending.delete(id);reject(new Error('Browser command timeout'));}, 5000);
      this.pending.set(id, {resolve,reject,timer});
      this.socket.send(JSON.stringify({id,method,params}));
    });
  }
}
(async () => {
  const child = spawn('C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe', [
    '--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
    '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
    '--autoplay-policy=no-user-gesture-required',
    `--use-file-for-fake-audio-capture=${fixture}`,
    `--user-data-dir=${path.join(root,'artifacts',`media-profile-${language}`)}`,
    `--remote-debugging-port=${port}`, `http://localhost:8099/?language=${language}`,
  ], {windowsHide:true,stdio:'ignore'});
  let client;
  try {
    for (let i=0;i<30;i++) {
      try {
        const tabs = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
        const tab=tabs.find(t=>t.url.startsWith('http://localhost:8099/'));
        if (tab) {client=new Client(tab.webSocketDebuggerUrl);await client.ready;break;}
      } catch {}
      await pause(500);
    }
    if (!client) throw new Error('Probe page unavailable');
    for(let i=0;i<65;i++) {
      const result=await client.call('Runtime.evaluate',{
        expression:'JSON.stringify(window.atlasProbeResult || null)',returnByValue:true});
      const text=result?.result?.value;
      if (text && text !== 'null') {
        const report=JSON.parse(text);
        fs.writeFileSync(path.join(root,'artifacts',`browser-${language}-capture.json`),JSON.stringify(report,null,2));
        console.log(JSON.stringify(report));
        if (!report.passed) process.exitCode=1;
        return;
      }
      await pause(1000);
    }
    throw new Error('Probe timed out');
  } finally {
    if (client) {
      try {await client.call('Browser.close');} catch {}
      client.socket.close();
    } else {child.kill();}
  }
})().catch(e=>{console.error(e.message);process.exitCode=1;});
