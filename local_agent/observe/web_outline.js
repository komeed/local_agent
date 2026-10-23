// Fixed, read-only outline of the page in the front tab (docs/PLAN_browser.md, "Reading the page").
// Runs as a function body: `args` = {max: element cap}. Returns plain data; the Python side maps it to UIElements.
// Reads no cookies or storage and never returns the value of a password field. Only side effect: a data-la-id
// attribute stamped on each listed element so the same control keeps its id while the page lives.
var MAX = (args && args.max) || 400;
var vw = window.innerWidth, vh = window.innerHeight;
var INTERACTIVE = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[contenteditable=""],[contenteditable="true"],' +
  '[role=button],[role=link],[role=tab],[role=checkbox],[role=radio],[role=switch],[role=menuitem],[role=option],' +
  '[role=combobox],[role=textbox],[role=searchbox],[tabindex]:not([tabindex^="-"])';
var TEXT = 'h1,h2,h3,h4,h5,h6,[role=heading],p,li,td,th,label,legend,figcaption,dt,dd,span,div,strong,em,b,i,small';
var ROLE_OK = {button:1, link:1, tab:1, checkbox:1, radio:1, "switch":1, menuitem:1, option:1, combobox:1, textbox:1,
  searchbox:1, heading:1};

function clean(s, n) { s = (s || "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
function hidden(el) {
  if (el.closest('[aria-hidden="true"],[hidden]')) return true;
  if (el.checkVisibility) return !el.checkVisibility({opacityProperty: true, visibilityProperty: true});
  for (var e = el; e && e.nodeType === 1; e = e.parentElement) {
    var cs = getComputedStyle(e);
    if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity) === 0) return true;
  }
  return false;
}
function inView(r) { return r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0 && r.left < vw && r.top < vh; }
function onTop(el, r) {  // not covered by an overlay (a modal over the same buttons)
  var x = Math.min(vw - 1, Math.max(0, r.left + r.width / 2)), y = Math.min(vh - 1, Math.max(0, r.top + r.height / 2));
  var hit = document.elementFromPoint(x, y);
  return !hit || hit === el || el.contains(hit) || hit.contains(el);
}
function roleOf(el) {
  var r = (el.getAttribute("role") || "").split(" ")[0].toLowerCase();
  if (r === "textbox" && el.isContentEditable) return "textarea";  // a rich editor takes text at the cursor
  if (ROLE_OK[r]) return r;
  var t = el.tagName.toLowerCase();
  if (t === "a") return "link";
  if (t === "button" || t === "summary") return "button";
  if (t === "select") return "combobox";
  if (t === "textarea") return "textarea";
  if (t === "input") {
    var ty = (el.type || "text").toLowerCase();
    if (ty === "button" || ty === "submit" || ty === "reset" || ty === "image") return "button";
    if (ty === "checkbox" || ty === "radio") return ty;
    if (ty === "password") return "password";
    if (ty === "search") return "searchbox";
    if (ty === "range") return "slider";
    return "textbox";
  }
  if (el.isContentEditable) return "textarea";
  if (/^h[1-6]$/.test(t)) return "heading";
  return "link";  // tabindex-only elements behave like clickable things
}
function nameOf(el, role) {
  var s = el.getAttribute("aria-label");
  if (s && s.trim()) return clean(s, 80);
  var by = el.getAttribute("aria-labelledby");
  if (by) {
    s = by.split(/\s+/).map(function (id) { var n = document.getElementById(id); return n ? n.textContent : ""; }).join(" ");
    if (s.trim()) return clean(s, 80);
  }
  if (el.labels && el.labels.length) {
    s = Array.prototype.map.call(el.labels, function (l) { return l.textContent; }).join(" ");
    if (s.trim()) return clean(s, 80);
  }
  var field = role === "textbox" || role === "textarea" || role === "searchbox" || role === "password" || role === "slider";
  if (!field) {
    if (el.tagName === "INPUT" && /^(button|submit|reset)$/.test(el.type)) s = el.value || (el.type === "submit" ? "Submit" : "");
    else s = el.innerText || el.textContent;
    if (s && s.trim()) return clean(s, 80);
    var img = el.querySelector && el.querySelector("img[alt],svg title");
    if (img) { s = img.getAttribute ? img.getAttribute("alt") : ""; s = s || img.textContent; if (s && s.trim()) return clean(s, 80); }
  }
  s = el.getAttribute("title") || el.getAttribute("alt");
  if (s && s.trim()) return clean(s, 80);
  return clean(el.getAttribute("placeholder"), 80);
}
function valueOf(el, role) {
  if (role === "password") return "";
  if (el.tagName === "SELECT") { var o = el.options[el.selectedIndex]; return o ? clean(o.text, 80) : ""; }
  if (el.isContentEditable || el.getAttribute("role") === "textbox") return clean(el.innerText, 200);
  if (role === "textbox" || role === "textarea" || role === "searchbox" || role === "slider") return clean(el.value, 200);
  return "";
}
function pathAndContext(el) {
  var segs = [], ctx = "";
  for (var p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
    var tag = p.tagName.toLowerCase(), r = p.getAttribute("role") || "", lab = p.getAttribute("aria-label");
    if (!ctx && (tag === "tr" || tag === "li" || r === "row" || r === "listitem")) {
      var rn = clean(p.getAttribute("aria-label") || p.innerText, 60);
      if (rn) ctx = 'row "' + rn + '"';
    }
    if (tag === "form" || r === "form") {
      segs.unshift("AXForm:" + clean(lab || p.getAttribute("name") || p.id || "", 40));
    } else if (tag === "dialog" || r === "dialog" || r === "alertdialog" || p.getAttribute("aria-modal") === "true") {
      var h = p.querySelector("h1,h2,h3,[role=heading]");
      segs.unshift("AXDialog:" + clean(lab || (h && h.textContent) || "", 40));
    } else if (tag === "fieldset") {
      var lg = p.querySelector("legend");
      segs.unshift("AXGroup:" + clean(lab || (lg && lg.textContent) || "", 40));
    } else if (lab && (/^(nav|header|footer|main|aside|section|form)$/.test(tag) || /^(navigation|banner|main|complementary|region|form|search|toolbar|tablist)$/.test(r))) {
      segs.unshift("AXGroup:" + clean(lab, 40));
    }
  }
  if (!ctx) {  // the nearest heading above the element
    var hs = document.querySelectorAll("h1,h2,h3,h4,[role=heading]"), best = null;
    for (var i = 0; i < hs.length; i++) {
      if (hs[i].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING && !hs[i].contains(el)) best = hs[i]; else break;
    }
    if (best && clean(best.textContent, 50)) ctx = 'section "' + clean(best.textContent, 50) + '"';
  }
  return {path: segs.join(" > "), context: ctx};
}
var nextId = window.__laNext || 1;
function stamp(el) {
  var id = el.getAttribute("data-la-id");
  if (!id) { id = "la" + (nextId++); el.setAttribute("data-la-id", id); }
  return id;
}

var items = [], interactiveSeen = 0, offscreen = 0, candidates = 0;
var nodes = document.querySelectorAll(INTERACTIVE);
candidates += nodes.length;
var inter = new Set();
for (var i = 0; i < nodes.length; i++) {
  var el = nodes[i], r = el.getBoundingClientRect();
  if (el.tagName === "INPUT" && el.type === "hidden") continue;
  if (!inView(r)) { if (r.width > 0 || r.height > 0) offscreen++; continue; }
  if (hidden(el) || !onTop(el, r)) continue;
  inter.add(el);
  interactiveSeen++;
  if (items.length >= MAX) continue;
  var role = roleOf(el), pc = pathAndContext(el);
  items.push({web_id: stamp(el), role: role, name: nameOf(el, role), value: valueOf(el, role),
    placeholder: clean(el.getAttribute("placeholder"), 60), tag: el.tagName.toLowerCase(),
    enabled: !(el.disabled || el.getAttribute("aria-disabled") === "true"), focused: el === document.activeElement,
    checked: el.checked === true || el.getAttribute("aria-checked") === "true" ? true : (el.getAttribute("aria-checked") === "false" || el.type === "checkbox" || el.type === "radio" ? false : null),
    rect: [Math.round(r.left * 10) / 10, Math.round(r.top * 10) / 10, Math.round(r.width * 10) / 10, Math.round(r.height * 10) / 10],
    path: pc.path, context: pc.context, interactive: true, href: role === "link" && el.href ? String(el.href).slice(0, 300) : "",
    required: el.required === true || el.getAttribute("aria-required") === "true",
    hint: [el.getAttribute("autocomplete"), el.getAttribute("name"), el.type].filter(Boolean).join(" ").slice(0, 80),
    submit: (el.tagName === "BUTTON" && (el.type === "submit")) || (el.tagName === "INPUT" && el.type === "submit")});
}
// Headings and short visible text (the same split as AXStaticText): elements whose own text is the content.
var texts = document.querySelectorAll(TEXT), textRoom = Math.max(0, MAX - items.length), nText = 0;
candidates += texts.length;
for (var j = 0; j < texts.length && nText < textRoom; j++) {
  var t = texts[j], own = "";
  for (var c = t.firstChild; c; c = c.nextSibling) if (c.nodeType === 3) own += c.nodeValue;
  var isHead = /^H[1-6]$/.test(t.tagName) || t.getAttribute("role") === "heading";
  var txt = clean(isHead ? t.textContent : own, 120);
  if (!txt || t.closest("script,style,noscript,template")) continue;
  var tr = t.getBoundingClientRect();
  if (!inView(tr)) continue;
  var up = t.parentElement && t.closest(INTERACTIVE);
  if (up) continue;  // text inside a control is that control's name
  if (hidden(t) || !onTop(t, tr)) continue;
  var pc2 = pathAndContext(t);
  items.push({web_id: "", role: isHead ? "heading" : "text", name: "", value: txt, placeholder: "", tag: t.tagName.toLowerCase(),
    enabled: true, focused: false, checked: null,
    rect: [Math.round(tr.left * 10) / 10, Math.round(tr.top * 10) / 10, Math.round(tr.width * 10) / 10, Math.round(tr.height * 10) / 10],
    path: pc2.path, context: "", interactive: false});
  nText++;
}
window.__laNext = nextId;
return {url: location.href, title: document.title, inner_width: vw, inner_height: vh,
  candidates: candidates, listed: items.length, interactive_in_view: interactiveSeen, more: offscreen + Math.max(0, interactiveSeen - items.filter(function (x) { return x.interactive; }).length),
  elements: items};
