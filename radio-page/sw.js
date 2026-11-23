/* RADIO BEHIX offline shell: cache the page itself so it opens
   with no connection. The worker caches ONLY the page document —
   never the live stream, never /nowplaying, never /track. Songs
   for offline listening are cached separately by the page itself
   (Cache Storage, "radio-behix-offline-v1"). First visit must be
   online, so the shell and the songs can be cached.

   UPDATE RULE (fixed 2026-11-23): the shell is fetched from the
   NETWORK first on every visit, so a new page version reaches the
   phone immediately. The cache is only the offline fallback — the
   old cache-first version kept serving the stale page forever,
   which is why the timer fix did not reach phones. */
var SHELL_CACHE = "radio-behix-shell-v2";
var OFFLINE_CACHE = "radio-behix-offline-v1";

self.addEventListener("install", function(e){
  e.waitUntil(
    caches.open(SHELL_CACHE).then(function(c){
      return c.addAll(["./", "index.html"]);
    }).then(function(){ return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function(e){
  e.waitUntil(
    caches.keys().then(function(keys){
      return Promise.all(keys.filter(function(k){
        return k !== SHELL_CACHE && k !== OFFLINE_CACHE;
      }).map(function(k){ return caches.delete(k); }));
    }).then(function(){ return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function(e){
  var url = new URL(e.request.url);
  var isShell = e.request.mode === "navigate" ||
    (url.origin === self.location.origin &&
     (url.pathname === "/" || url.pathname.endsWith("/index.html")));
  if (!isShell) return; /* streams, polls, tracks: network only */
  e.respondWith(
    fetch(e.request).then(function(res){
      var copy = res.clone();
      caches.open(SHELL_CACHE).then(function(c){ c.put(e.request, copy); });
      return res;
    }).catch(function(){
      return caches.match(e.request).then(function(hit){
        return hit || caches.match("index.html");
      });
    })
  );
});
