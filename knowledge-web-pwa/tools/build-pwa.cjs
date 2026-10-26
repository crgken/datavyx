const {generateSW} = require('workbox-build');
(async () => {
  const result = await generateSW({
    globDirectory: 'build/web',
    globPatterns: ['**/*'],
    globIgnores: ['sw.js', 'workbox-*.js', 'flutter_service_worker.js', '**/*.map', 'version.json'],
    swDest: 'build/web/sw.js',
    maximumFileSizeToCacheInBytes: 32 * 1024 * 1024,
    skipWaiting: false,
    clientsClaim: true,
    cleanupOutdatedCaches: true,
    navigateFallback: 'index.html',
    navigateFallbackDenylist: [/^\/api\//],
    runtimeCaching: [{urlPattern: ({url}) => url.pathname.startsWith('/api/'), handler: 'NetworkOnly'}],
  });
  console.log(`PWA cached ${result.count} static assets (${result.size} bytes)`);
  for (const warning of result.warnings) console.warn(warning);
})().catch(() => { console.error('PWA build failed'); process.exitCode = 1; });
