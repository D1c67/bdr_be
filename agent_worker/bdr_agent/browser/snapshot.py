"""Page snapshot for the model: an accessibility-style outline with ref_N
handles (spec 4.1 read_page / find_text).

The walker runs in the page (executor-internal; the model never gets
JavaScript evaluation). It tags each interactive element with
`data-bdr-ref="<nonce>:<n>"`, where the nonce changes on every snapshot, so a
ref from an older snapshot (or one a page planted itself) never resolves:
the executor reports `stale_ref` instead of clicking the wrong thing.

Values are never shown for password inputs, one-time-code inputs, inputs
whose name, id, label or placeholder looks like a secret
(protocol.SECRET_INPUT_RE), or any element the executor filled from a
secret: they read `[hidden]`. Page text is untrusted; BDR wraps it before it
reaches the model, and the executor scrubs live secrets from it first.
"""

import secrets as pysecrets
from dataclasses import dataclass, field
from typing import Any

from ..protocol import SECRET_INPUT_RE

REF_ATTR = "data-bdr-ref"
DEFAULT_MAX_CHARS = 8000
MAX_NODES = 4000
HIDDEN = "[hidden]"

# Everything the page shows for a secret-ish field is hidden and every such
# field is masked in screenshots (executor.MASK_SELECTORS uses the same words).
_SECRET_WORDS_JS = SECRET_INPUT_RE.pattern.replace("(?i)", "")

_WALKER_JS = r"""
(args) => {
  const ATTR = args.attr, NONCE = args.nonce, MAX = args.maxNodes;
  const SECRET_RE = new RegExp(args.secretWords, 'i');
  const secretEls = new Set();
  for (const sel of args.secretSelectors || []) {
    try { document.querySelectorAll(sel).forEach(e => secretEls.add(e)); } catch (e) {}
  }
  document.querySelectorAll('[' + ATTR + ']').forEach(e => e.removeAttribute(ATTR));
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'META', 'LINK', 'SVG', 'IFRAME', 'OBJECT', 'EMBED']);
  const clean = (s, n) => (s || '').replace(/[​-‏‪-‮⁦-⁩]/g, '').replace(/\s+/g, ' ').trim().slice(0, n);
  const visible = (el) => {
    if (el.getAttribute('aria-hidden') === 'true') return false;
    if (!el.getClientRects().length) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const labelText = (el) => {
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map(id => { const n = document.getElementById(id); return n ? n.textContent : ''; }).join(' ');
      if (clean(t, 200)) return clean(t, 200);
    }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'), 200);
    if (el.id) {
      try {
        const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l) return clean(l.textContent, 200);
      } catch (e) {}
    }
    const wrap = el.closest('label');
    if (wrap) return clean(wrap.textContent, 200);
    return '';
  };
  const isSecret = (el) => {
    if (secretEls.has(el)) return true;
    const tag = el.tagName;
    if (tag !== 'INPUT' && tag !== 'TEXTAREA') return false;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'password') return true;
    if ((el.getAttribute('autocomplete') || '').toLowerCase() === 'one-time-code') return true;
    const words = [el.getAttribute('name'), el.id, el.getAttribute('placeholder'), el.getAttribute('aria-label'), labelText(el)].join(' ');
    return SECRET_RE.test(words);
  };
  const roleOf = (el) => {
    const r = el.getAttribute('role');
    if (r) return r;
    const tag = el.tagName;
    if (tag === 'A' && el.hasAttribute('href')) return 'link';
    if (tag === 'BUTTON' || tag === 'SUMMARY') return 'button';
    if (tag === 'SELECT') return 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'INPUT') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'hidden') return null;
      if (['button', 'submit', 'reset', 'image'].includes(t)) return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'search') return 'searchbox';
      if (t === 'file') return 'file';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    if (el.hasAttribute('onclick') || el.hasAttribute('tabindex') && el.getAttribute('tabindex') !== '-1') return 'button';
    return null;
  };
  const INTERACTIVE = new Set(['link', 'button', 'combobox', 'textbox', 'checkbox', 'radio', 'searchbox', 'tab', 'menuitem', 'option', 'switch', 'listbox']);
  const lines = [], refs = {};
  let n = 0, nodes = 0, truncated = false;
  const walk = (el, depth) => {
    if (truncated) return;
    if (++nodes > MAX) { truncated = true; return; }
    if (SKIP.has(el.tagName)) return;
    if (!visible(el)) return;
    const role = roleOf(el);
    const indent = '  '.repeat(Math.min(depth, 8));
    if (role && INTERACTIVE.has(role) && role !== 'file') {
      n += 1;
      const ref = 'ref_' + n;
      el.setAttribute(ATTR, NONCE + ':' + n);
      const secret = isSecret(el);
      let name = labelText(el) || clean(el.getAttribute('placeholder'), 120) || clean(el.getAttribute('title'), 120) || clean(el.getAttribute('alt'), 120);
      if (!name && !['textbox', 'searchbox', 'combobox'].includes(role)) name = clean(el.innerText || el.value, 120);
      let line = indent + '[' + ref + '] ' + role + (name ? ' "' + name + '"' : '');
      if (role === 'textbox' || role === 'searchbox') {
        const v = el.isContentEditable ? el.innerText : el.value;
        if (secret) line += ' value="[hidden]"';
        else if (v) line += ' value="' + clean(v, 200) + '"';
      } else if (role === 'combobox' && el.tagName === 'SELECT') {
        const opt = el.options[el.selectedIndex];
        if (opt) line += ' value="' + clean(opt.textContent, 120) + '"';
        const opts = Array.from(el.options).slice(0, 25).map(o => clean(o.textContent, 60));
        line += ' options=' + JSON.stringify(opts);
      } else if (role === 'checkbox' || role === 'radio') {
        line += el.checked ? ' checked' : ' unchecked';
      }
      if (el.disabled) line += ' disabled';
      if (role === 'link') {
        const href = el.getAttribute('href') || '';
        if (href && !href.startsWith('javascript:')) line += ' href="' + clean(href.split('?')[0].split('#')[0], 300) + '"';
      }
      lines.push(line);
      refs[ref] = {
        tag: el.tagName.toLowerCase(), role: role, secret: secret,
        type: (el.getAttribute('type') || '').toLowerCase(),
      };
      // The line already carries the element's name; its children add nothing.
      return;
    } else {
      const tag = el.tagName;
      let own = '';
      for (const c of el.childNodes) if (c.nodeType === 3) own += c.textContent;
      own = clean(own, 400);
      if (/^H[1-6]$/.test(tag)) {
        lines.push(indent + 'heading(' + tag[1] + ') "' + clean(el.innerText, 300) + '"');
        return;
      }
      if (own) lines.push(indent + 'text "' + own + '"');
    }
    for (const c of el.children) walk(c, depth + 1);
  };
  if (document.body) walk(document.body, 0);
  return {lines: lines, refs: refs, truncated: truncated, title: clean(document.title, 300), url: location.href};
}
"""


@dataclass
class RefInfo:
    tag: str
    role: str
    secret: bool
    type: str = ""


@dataclass
class Snapshot:
    nonce: str
    url: str
    title: str
    lines: list[str]
    refs: dict[str, RefInfo] = field(default_factory=dict)
    truncated: bool = False

    def text(self, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str, bool]:
        """The outline, cut at `max_chars` on a line boundary."""
        out: list[str] = []
        used = 0
        cut = self.truncated
        for line in self.lines:
            if used + len(line) + 1 > max_chars:
                cut = True
                break
            out.append(line)
            used += len(line) + 1
        return "\n".join(out), cut

    def selector(self, ref: str) -> str | None:
        if ref not in self.refs:
            return None
        return f'[{REF_ATTR}="{self.nonce}:{ref.removeprefix("ref_")}"]'

    def find(self, query: str, limit: int = 20) -> list[str]:
        q = query.casefold()
        return [ln.strip() for ln in self.lines if q in ln.casefold()][:limit]


def _hide_secret_values(lines: list[str]) -> list[str]:
    """Belt and braces: the walker already hides these; a page that patched
    DOM APIs must still not get a value out next to a password word."""
    out = []
    for ln in lines:
        if ' value="' in ln and SECRET_INPUT_RE.search(ln.split(' value="', 1)[0]):
            head = ln.split(' value="', 1)[0]
            ln = f'{head} value="{HIDDEN}"'
        out.append(ln)
    return out


async def take_snapshot(page: Any, *, secret_selectors: list[str] | tuple[str, ...] = ()) -> Snapshot:
    nonce = pysecrets.token_hex(6)
    data = await page.evaluate(_WALKER_JS, {
        "attr": REF_ATTR, "nonce": nonce, "maxNodes": MAX_NODES,
        "secretWords": _SECRET_WORDS_JS, "secretSelectors": list(secret_selectors),
    })
    lines = [str(x) for x in (data.get("lines") or [])]
    refs = {}
    for k, v in (data.get("refs") or {}).items():
        if isinstance(v, dict):
            refs[str(k)] = RefInfo(tag=str(v.get("tag") or ""), role=str(v.get("role") or ""),
                                   secret=bool(v.get("secret")), type=str(v.get("type") or ""))
    return Snapshot(nonce=nonce, url=str(data.get("url") or page.url), title=str(data.get("title") or ""),
                    lines=_hide_secret_values(lines), refs=refs, truncated=bool(data.get("truncated")))
