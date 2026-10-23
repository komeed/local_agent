// Fixed page actions (docs/PLAN_browser.md, "Acting on the page"). Runs as a function body; `args` =
// {op, web_id, text?, direction?}. The planner never supplies any of this code; `web_id` is a data-la-id the
// outline script stamped. Returns plain data: {ok, ...}; {ok:false, gone:true} when the element left the DOM.
// Reads no cookies or storage. Never types into a password field.
(function () {  // one MutationObserver per page, so "has the page settled" can be asked after any action
  if (!window.__laMut) {
    window.__laMut = {last: Date.now()};
    new MutationObserver(function () { window.__laMut.last = Date.now(); })
      .observe(document.documentElement, {subtree: true, childList: true, attributes: true, characterData: true});
  }
})();
var op = args.op;
if (op === "settle") {
  return {ready: document.readyState === "complete", quiet_ms: Date.now() - window.__laMut.last};
}
var el = args.web_id ? document.querySelector('[data-la-id="' + String(args.web_id).replace(/[^a-z0-9_-]/gi, "") + '"]') : null;
function rectOf(e) {
  var r = e.getBoundingClientRect();
  return [r.left, r.top, r.width, r.height];
}
function scrollable(e) {
  for (var p = e; p && p !== document.documentElement; p = p.parentElement) {
    var oy = getComputedStyle(p).overflowY;
    if ((oy === "auto" || oy === "scroll") && p.scrollHeight > p.clientHeight + 1) return p;
  }
  return null;
}
function fire(e, name) { e.dispatchEvent(new Event(name, {bubbles: true})); }
function setNative(e, value) {
  var proto = e.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value").set.call(e, value);  // React/Vue only see this path
}
if (op === "scroll") {
  var dir = args.direction || "down";
  var box = el ? scrollable(el) : null;
  var target = box || document.scrollingElement || document.documentElement;
  var size = box ? box.clientHeight : window.innerHeight, wsize = box ? box.clientWidth : window.innerWidth;
  var dy = dir === "down" ? 0.8 * size : dir === "up" ? -0.8 * size : 0;
  var dx = dir === "right" ? 0.8 * wsize : dir === "left" ? -0.8 * wsize : 0;
  var before = [target.scrollTop, target.scrollLeft];
  target.scrollBy(dx, dy);
  return {ok: true, moved: target.scrollTop !== before[0] || target.scrollLeft !== before[1],
    where: box ? "element" : "window", top: target.scrollTop};
}
if (!el) return {ok: false, gone: true};
if (op === "rect") {
  el.scrollIntoView({block: "center", inline: "center"});
  return {ok: true, rect: rectOf(el), inner_width: window.innerWidth, inner_height: window.innerHeight};
}
if (op === "click") {
  el.scrollIntoView({block: "center", inline: "center"});
  el.click();
  return {ok: true};
}
if (op === "type") {
  var text = String(args.text == null ? "" : args.text);
  var tag = el.tagName;
  if ((tag === "INPUT" && el.type === "password") || el.closest("input[type=password]")) return {ok: false, secure: true};
  el.scrollIntoView({block: "center", inline: "center"});
  el.focus();
  if (tag === "SELECT") {  // choose the option whose text (or value) is `text`
    var want = text.trim().toLowerCase(), found = null;
    for (var oi = 0; oi < el.options.length; oi++) {
      var opt = el.options[oi];
      if (opt.text.trim().toLowerCase() === want || String(opt.value).toLowerCase() === want) { found = opt; break; }
    }
    if (!found) return {ok: false, error: "no option named " + text.slice(0, 40)};
    el.value = found.value;
    fire(el, "input");
    fire(el, "change");
    return {ok: true, how: "chose", value: found.text.slice(0, 80)};
  }
  if (tag === "INPUT" || tag === "TEXTAREA") {
    if (tag === "INPUT") {  // one line: the text replaces the value
      setNative(el, text);
    } else {  // a text area takes text at the cursor
      var v = el.value, s = el.selectionStart == null ? v.length : el.selectionStart, e2 = el.selectionEnd == null ? s : el.selectionEnd;
      setNative(el, v.slice(0, s) + text + v.slice(e2));
      el.selectionStart = el.selectionEnd = s + text.length;
    }
    el.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: text}));
    fire(el, "change");
    return {ok: true, how: tag === "INPUT" ? "replaced" : "inserted", value: el.value.slice(0, 200)};
  }
  if (el.isContentEditable) {
    var sel = window.getSelection();
    if (!sel.rangeCount || !el.contains(sel.anchorNode)) {  // no cursor in the editor: put it at the end
      var range = document.createRange();
      range.selectNodeContents(el);
      range.collapse(false);
      sel.removeAllRanges();
      sel.addRange(range);
    }
    var inserted = document.execCommand("insertText", false, text);
    var now = (el.innerText || "").replace(/\s+/g, " ");
    var landed = inserted && now.indexOf(text.replace(/\s+/g, " ")) >= 0;
    return {ok: landed, fallback: !landed, how: "execCommand", value: now.slice(0, 200)};
  }
  return {ok: false, error: "not a text field"};
}
return {ok: false, error: "unknown op"};
