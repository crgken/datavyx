// Local development diagnostic, restricted to the isolated Flutter test page.
const port = Number(process.argv[2]);
(async () => {
  const tabs = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const tab = tabs.find(t => t.title === 'Flutter Test Browser Host');
  if (!tab) throw new Error('Test page not found');
  const socket = new WebSocket(tab.webSocketDebuggerUrl);
  const timer = setTimeout(() => {socket.close(); process.exitCode = 1;}, 8000);
  socket.addEventListener('open', () => socket.send(JSON.stringify({id: 1, method: 'Runtime.evaluate', params: {
    expression: `JSON.stringify({text:document.body.innerText.slice(0,1200),frames:[...document.querySelectorAll('iframe')].map(f=>({state:f.contentDocument?.readyState,text:f.contentDocument?.body.innerText.slice(0,400),scripts:[...f.contentDocument.scripts].map(s=>s.src),resources:f.contentWindow.performance.getEntriesByType('resource').map(r=>({name:r.name.split('?')[0],duration:r.duration})).slice(-10)})),resources:performance.getEntriesByType('resource').map(r=>({name:r.name.split('?')[0],duration:r.duration})).slice(-12)})`,
    returnByValue: true,
  }})));
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id === 1) {
      console.log(message.result?.result?.value || 'No diagnostic data');
      clearTimeout(timer); socket.close();
    }
  });
})().catch(() => {console.error('Local test diagnostic unavailable');process.exitCode=1;});
