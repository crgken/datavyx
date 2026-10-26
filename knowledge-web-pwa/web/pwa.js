(() => {
  let promptEvent;
  let registration;
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    promptEvent = event;
  });
  window.atlasPwaInstall = async () => {
    if (window.matchMedia('(display-mode: standalone)').matches) return 'installed';
    if (!promptEvent) return 'manual';
    await promptEvent.prompt();
    const choice = await promptEvent.userChoice;
    promptEvent = null;
    return choice.outcome;
  };
  window.atlasPwaUpdate = () => {
    if (registration?.waiting) registration.waiting.postMessage({type: 'SKIP_WAITING'});
  };
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', async () => {
      try {
        registration = await navigator.serviceWorker.register('/sw.js');
        const announce = () => {
          window.atlasUpdateReady = true;
          window.dispatchEvent(new Event('atlas-update-ready'));
        };
        if (registration.waiting) announce();
        registration.addEventListener('updatefound', () => {
          const worker = registration.installing;
          worker?.addEventListener('statechange', () => {
            if (worker.state === 'installed' && navigator.serviceWorker.controller) announce();
          });
        });
      } catch {
        // Installation may be unavailable, but online operation remains usable.
        window.dispatchEvent(new Event('atlas-install-unavailable'));
      }
    });
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (window.atlasUpdateRequested) location.reload();
    });
  }
})();
