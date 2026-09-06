/**
 * Offline for a published app — what makes Add to Home Screen honest.
 *
 * The reader taps the icon on a train and the app opens. Without this, an
 * installed app is a bookmark that shows a dinosaur.
 *
 * Cache-first, with the cache name carrying the build id the site was published
 * with. A re-publish mints a new id, so the new worker fills a NEW cache and
 * deletes the old ones on activate: readers get the update, and nobody is served
 * a half-old half-new mix of the app's files and its Python.
 *
 * It caches only same-origin GETs, and it never caches an error response — a
 * 404 pinned into the cache would outlive the mistake that caused it.
 *
 * The reader's own data is NOT here. It lives in localStorage (see
 * _fused/runtime.js), which the cache lifecycle does not touch — which is why a
 * re-publish, and the cache wipe it triggers, leaves their progress alone.
 */
const CACHE = "fused-site-__BUILD_ID__";
const PRECACHE = __PRECACHE__;

self.addEventListener("install", (event) => {
  // Take over as soon as the new bytes are in: the author published an update
  // because they wanted it seen, and a worker waiting for every tab to close can
  // sit unused for days.
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  event.respondWith(
    caches.match(request).then((hit) => {
      if (hit) return hit;
      return fetch(request).then((response) => {
        if (response && response.ok && response.type === "basic") {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      });
    })
  );
});
