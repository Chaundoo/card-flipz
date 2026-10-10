/* Chaundoo Card Flipz offline helper.
   - The page itself opens from the phone's storage right away, then quietly checks GitHub for a newer copy.
   - versions.json (the "what changed" list) is asked for fresh, but if the signal is slow or gone
     the last copy is used, so the site still opens with the saved card data.
   - Card/pairing/log files are requested as file.json?v=<id>. A given id never changes, so a saved copy
     is used without asking GitHub at all; a new id means new data and gets downloaded once. */
var CACHE = "cfz-v1";
var SLOW_MS = 3500;

self.addEventListener("install", function(e){
  e.waitUntil(caches.open(CACHE).then(function(c){ return c.addAll(["./", "./index.html"]).catch(function(){}); }).then(function(){ return self.skipWaiting(); }));
});
self.addEventListener("activate", function(e){
  e.waitUntil(caches.keys().then(function(keys){
    return Promise.all(keys.filter(function(k){ return k.indexOf("cfz-")===0 && k!==CACHE; }).map(function(k){ return caches.delete(k); }));
  }).then(function(){ return self.clients.claim(); }));
});

function put(req, res){ if(res && res.ok){ var copy = res.clone(); caches.open(CACHE).then(function(c){ c.put(req, copy); }); } return res; }
function marked(res){ /* tell the page this answer came from the phone, not GitHub */
  return res.blob().then(function(b){ var h = new Headers(res.headers); h.set("X-From-Phone", "1"); return new Response(b, {status:res.status, headers:h}); });
}

self.addEventListener("fetch", function(e){
  var req = e.request; if(req.method!=="GET") return;
  var url = new URL(req.url); if(url.origin!==location.origin) return;
  var base = new URL("./", self.registration.scope).pathname, path = url.pathname;

  /* the page: saved copy first, refresh in the background */
  if(req.mode==="navigate" || path===base || path===base+"index.html"){
    var key = base+"index.html";
    e.respondWith(caches.match(key).then(function(hit){
      var net = fetch(req, {cache:"no-cache"}).then(function(r){ return put(key, r); });
      if(hit){ e.waitUntil(net.catch(function(){})); return hit; }
      return net;
    }));
    return;
  }

  /* the "what changed" list: fresh if GitHub answers within a few seconds, else the saved one */
  if(path===base+"versions.json"){
    var vkey = base+"versions.json";
    e.respondWith(new Promise(function(resolve){
      var done = false;
      function fallback(){ if(done) return; caches.match(vkey).then(function(hit){ if(done) return; if(hit){ done = true; resolve(marked(hit)); } }); }
      var timer = setTimeout(fallback, SLOW_MS);
      fetch(req, {cache:"no-store"}).then(function(r){ put(vkey, r.clone()); if(!done){ done = true; clearTimeout(timer); resolve(r); } })
        .catch(function(){ clearTimeout(timer); caches.match(vkey).then(function(hit){ if(done) return; done = true; resolve(hit ? marked(hit) : Response.error()); }); });
    }));
    return;
  }

  /* data files with a version id: never change, so the saved copy is always right */
  if(/\.json$/.test(path) && url.searchParams.has("v")){
    e.respondWith(caches.match(req.url).then(function(hit){
      if(hit) return hit;
      return fetch(req).then(function(r){
        if(r.ok){ var copy = r.clone(); caches.open(CACHE).then(function(c){
          /* drop older versions of the same file */
          return c.keys().then(function(ks){ ks.forEach(function(k){ var u = new URL(k.url); if(u.pathname===path && k.url!==req.url) c.delete(k); }); }).then(function(){ return c.put(req.url, copy); });
        }); }
        return r;
      });
    }));
    return;
  }
});
