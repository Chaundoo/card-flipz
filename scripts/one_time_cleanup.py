#!/usr/bin/env python3
"""One-time patch (run from the Actions tab; safe to run again).

Adds the "+ Add from TCGplayer link" button to index.html and lowers the daily
price bot's API limit to 80 requests (leaves room for adding cards).
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHES = {
    'index.html': [
        (r""".chooser p{margin:0 0 16px;color:var(--muted);font-size:14px}
.choices{display:grid;grid-template-columns:1fr 1fr;gap:12px}
""",
         r""".chooser p{margin:0 0 16px;color:var(--muted);font-size:14px}
.addlink{text-align:left;max-width:460px}
.addlink label{display:block;font-size:13px;font-weight:700;margin:12px 0 4px}
.addlink input,.addlink select{width:100%;box-sizing:border-box;padding:10px;border:1px solid var(--line);border-radius:10px;font:inherit;background:var(--bg);color:var(--ink)}
.addlink .row{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
.addlink .msg{margin-top:12px;font-size:14px}
.addlink .msg.err{color:var(--neg)}
.choices{display:grid;grid-template-columns:1fr 1fr;gap:12px}
"""),
        (r"""    '<button class="advbtn" id="advbtn" role="switch" aria-checked="'+ADV+'" title="Show grading values, population counts and break-even grades"><i aria-hidden="true"></i>Advanced analytics</button>'+
    (canWrite!==false && artifactApi ? '<button class="btn primary" id="add">+ Add card</button>' : (apiChecked ? '<span class="readonly-note">View only: ask the owner for edit access to add cards.</span>' : ''))+
  '</div>';
""",
         r"""    '<button class="advbtn" id="advbtn" role="switch" aria-checked="'+ADV+'" title="Show grading values, population counts and break-even grades"><i aria-hidden="true"></i>Advanced analytics</button>'+
    '<button class="btn primary" id="addlink">+ Add from TCGplayer link</button>'+
    (canWrite!==false && artifactApi ? '<button class="btn" id="add">+ Add card by hand</button>' : '')+
  '</div>';
"""),
        (r"""  var add = document.getElementById("add"); if(add) add.addEventListener("click", function(){ openEditor(null); });
  document.getElementById("selbtn").addEventListener("click", function(){ state.selecting = !state.selecting; if(!state.selecting) selected = []; var y = window.scrollY; render(); window.scrollTo(0,y); });
""",
         r"""  var add = document.getElementById("add"); if(add) add.addEventListener("click", function(){ openEditor(null); });
  var addl = document.getElementById("addlink"); if(addl) addl.addEventListener("click", showAddLink);
  document.getElementById("selbtn").addEventListener("click", function(){ state.selecting = !state.selecting; if(!state.selecting) selected = []; var y = window.scrollY; render(); window.scrollTo(0,y); });
"""),
        (r"""
/* ---------- routing ---------- */
""",
         r"""
/* ---------- add a card from a TCGplayer link ----------
   The page never contacts TCGplayer or the price API (that would expose the API key).
   It opens a ready-made GitHub issue; the "Add card from TCGplayer link" GitHub job looks the card up
   on TCG Price Lookup, checks for duplicates, adds it to the right era file and replies on the issue. */
var ADD_REPO = "Chaundoo/card-flipz";
var PRINTINGS = ["Auto (from the link)","Holofoil","Reverse Holofoil","Normal","1st Edition Holofoil","1st Edition","Unlimited Holofoil","Unlimited"];
function parseTcgLink(u){
  var m = String(u||"").trim().match(/tcgplayer\.com\/product\/(\d+)(?:\/([^?#\s]*))?(?:\?([^#\s]*))?/i);
  if(!m) return null;
  var pr = ""; (m[3]||"").split("&").forEach(function(kv){ var a = kv.split("="); if(a[0].toLowerCase()==="printing") pr = decodeURIComponent((a[1]||"").replace(/\+/g," ")); });
  return {id:m[1], slug:m[2]||"", printing:pr};
}
function printingOf(c){
  if(c.tcgVariant) return c.tcgVariant.toLowerCase();
  var l = (c.variant||"").toLowerCase(), holo = l.indexOf("holo")>=0 && l.indexOf("non-holo")<0;
  if(l.indexOf("reverse")>=0) return "reverse holofoil";
  if(l.indexOf("1st edition")>=0) return holo ? "1st edition holofoil" : "1st edition";
  if(l.indexOf("unlimited")>=0) return holo ? "unlimited holofoil" : "unlimited";
  if(l.indexOf("non-holo")>=0 || l.indexOf("normal")>=0) return "normal";
  return "holofoil";
}
function showAddLink(){
  if(document.getElementById("addlink-bg")) return;
  var d = document.createElement("div"); d.className="chooser-bg"; d.id="addlink-bg";
  d.innerHTML = '<div class="chooser addlink" role="dialog" aria-modal="true" aria-labelledby="al-t"><h2 id="al-t">Add a card from TCGplayer</h2>'+
    '<p>Paste the card\'s TCGplayer link. GitHub opens with it filled in: press <b>Create</b> and the card shows up here in about 2 minutes with its condition prices.</p>'+
    '<label for="al-url">TCGplayer link</label><input id="al-url" type="url" inputmode="url" placeholder="https://www.tcgplayer.com/product/85682/...">'+
    '<label for="al-pr">Printing</label><select id="al-pr">'+PRINTINGS.map(function(p,i){ return '<option value="'+(i?esc(p):"")+'">'+esc(p)+'</option>'; }).join("")+'</select>'+
    '<div class="msg" id="al-msg" role="status"></div>'+
    '<div class="row"><button class="btn" id="al-cancel">Cancel</button><button class="btn primary" id="al-go">Add card</button></div></div>';
  document.body.appendChild(d);
  var url = document.getElementById("al-url"), pr = document.getElementById("al-pr"), msg = document.getElementById("al-msg");
  function close(){ d.remove(); }
  function say(t, err){ msg.className = "msg"+(err?" err":""); msg.innerHTML = t; }
  document.getElementById("al-cancel").addEventListener("click", close);
  d.addEventListener("click", function(e){ if(e.target===d) close(); });
  document.getElementById("al-go").addEventListener("click", function(){
    var L = parseTcgLink(url.value);
    if(!L){ say("That doesn't look like a TCGplayer card link. It should contain tcgplayer.com/product/ and a number.", true); return; }
    var want = (pr.value || L.printing || "").toLowerCase();
    var same = cards.filter(function(c){ return String(c.tcgId||"")===L.id; });
    var dup = same.filter(function(c){ return !want || printingOf(c)===want; })[0];
    if(dup){ say('Already on the site: <a href="#'+esc(dup.id)+'" id="al-open">'+esc(dup.name)+' ('+esc(dup.variant||"")+')</a>. Pick a different printing if you meant another version.', true);
      document.getElementById("al-open").addEventListener("click", function(e){ e.preventDefault(); close(); go(dup.id); }); return; }
    var body = "TCGplayer link: "+url.value.trim()+"\nPrinting: "+(pr.value || L.printing || "auto")+"\n\nPress Create. The bot adds the card and replies here (about 2 minutes).";
    var gh = "https://github.com/"+ADD_REPO+"/issues/new?title="+encodeURIComponent("Add card: "+L.id)+"&body="+encodeURIComponent(body);
    window.open(gh, "_blank", "noopener");
    say("GitHub opened in a new tab. Press <b>Create</b> there, then refresh this page in about 2 minutes. If the card can't be added, the bot explains why on that GitHub page.");
  });
  url.focus();
}

/* ---------- routing ---------- */
"""),
    ],
    'scripts/refresh_prices.py': [
        (r"""    cards that have never been synced come first, then the ones synced longest ago.
  * It never uses more than DAILY_REQUESTS API requests (default 90 of the free
    plan's 100/day) and also stops when the API says the daily quota is used up.
  * Requests are spaced several seconds apart to respect the burst limit.
""",
         r"""    cards that have never been synced come first, then the ones synced longest ago.
  * It never uses more than DAILY_REQUESTS API requests (default 80 of the free
    plan's 100/day, leaving room for adding cards) and also stops when the API says the daily quota is used up.
  * Requests are spaced several seconds apart to respect the burst limit.
"""),
        (r"""  TCGPL_API_KEY   your API key (GitHub secret)                       required
  DAILY_REQUESTS  max API requests per run                           default 90
  DAILY_SHARE     cards per run (0 = total cards / 7, rounded up)    default 0
""",
         r"""  TCGPL_API_KEY   your API key (GitHub secret)                       required
  DAILY_REQUESTS  max API requests per run                           default 80
  DAILY_SHARE     cards per run (0 = total cards / 7, rounded up)    default 0
"""),
        (r"""BASE = (os.environ.get("TCGPL_BASE") or "https://api.tcgpricelookup.com/v1").rstrip("/")
DAILY_REQUESTS = int(os.environ.get("DAILY_REQUESTS") or 90)
DAILY_SHARE = int(os.environ.get("DAILY_SHARE") or 0)
""",
         r"""BASE = (os.environ.get("TCGPL_BASE") or "https://api.tcgpricelookup.com/v1").rstrip("/")
DAILY_REQUESTS = int(os.environ.get("DAILY_REQUESTS") or 80)   # leaves ~20/day for adding cards
DAILY_SHARE = int(os.environ.get("DAILY_SHARE") or 0)
"""),
    ],
}

for path, pairs in PATCHES.items():
    p = os.path.join(ROOT, path)
    s = open(p, encoding="utf-8").read()
    done = 0
    for old, new in pairs:
        if new in s:
            print(f"{path}: one change already applied")
        elif old in s:
            s = s.replace(old, new, 1)
            done += 1
        else:
            raise SystemExit(f"{path}: couldn't find the spot to change (file was edited?). Nothing saved.")
    open(p, "w", encoding="utf-8").write(s)
    print(f"{path}: {done} changes applied")
