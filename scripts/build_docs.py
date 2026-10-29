#!/usr/bin/env python3
"""
Render the Markdown setup guide to a single self-contained HTML page.

    pip install markdown
    python scripts/build_docs.py                                 # docs/HERMES_MAC_MINI_SETUP.md -> .html
    python scripts/build_docs.py docs/OTHER.md -o out.html       # any other doc
    python scripts/build_docs.py --fragment -o page.html         # body only (no <html>/<head> wrapper)

The page has a sticky table of contents, "Run as: admin / aiagents" badges, highlighted "Why" notes,
copy buttons on code blocks, tick-able checklists (remembered in the browser) and light/dark themes.
"""
from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent

CSS = r"""
/* Runbook layout: sticky contents rail on the left, a 72ch reading column on the right. */
:root {
  --bg: #f6f7f5; --surface: #ffffff; --ink: #1d2320; --muted: #5d6a63; --line: #dde3df;
  --accent: #1f6f5c; --accent-soft: #e3f1ec;
  --admin: #9a5b00; --admin-soft: #fdf1dc; --agents: #1f5f8b; --agents-soft: #e2eef8;
  --code-bg: #18201c; --code-ink: #e7efe9; --inline-code: #eef2ef;
  --font-display: 'Bricolage Grotesque', 'Avenir Next', system-ui, sans-serif;
  --font-body: 'Atkinson Hyperlegible', 'Segoe UI', system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #121614; --surface: #1a201d; --ink: #e4ebe7; --muted: #9db0a6; --line: #2c3631;
  --accent: #5cc4a6; --accent-soft: #1d3029;
  --admin: #f2b35a; --admin-soft: #33270f; --agents: #7cb8e6; --agents-soft: #16283a;
  --code-bg: #0c100e; --code-ink: #dbe6df; --inline-code: #232b27; color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #121614; --surface: #1a201d; --ink: #e4ebe7; --muted: #9db0a6; --line: #2c3631;
  --accent: #5cc4a6; --accent-soft: #1d3029;
  --admin: #f2b35a; --admin-soft: #33270f; --agents: #7cb8e6; --agents-soft: #16283a;
  --code-bg: #0c100e; --code-ink: #dbe6df; --inline-code: #232b27; color-scheme: dark; }

* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font-family: var(--font-body);
  font-size: 17px; line-height: 1.65; }
.shell { display: grid; grid-template-columns: 270px minmax(0, 1fr); gap: 48px; max-width: 1180px; margin: 0 auto;
  padding-block: 40px 80px; padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px)); }
.toc { position: sticky; top: calc(env(safe-area-inset-top, 0px) + 24px); align-self: start;
  max-height: calc(100vh - 48px); overflow-y: auto; font-size: 14px; }
.toc__title { font-family: var(--font-display); font-weight: 700; font-size: 13px; text-transform: uppercase;
  letter-spacing: 0.1em; color: var(--muted); margin: 0 0 10px; }
.toc ul { list-style: none; margin: 0; padding: 0; }
.toc li ul { padding-left: 14px; }
.toc a { display: block; padding: 4px 8px; border-radius: 6px; color: var(--muted); text-decoration: none; line-height: 1.35; }
.toc a:hover { color: var(--ink); background: var(--accent-soft); }
.toc a.is-active { color: var(--accent); font-weight: 700; }
main { min-width: 0; max-width: 76ch; }

h1, h2, h3 { font-family: var(--font-display); line-height: 1.2; text-wrap: balance; scroll-margin-top: 20px; }
h1 { font-size: clamp(30px, 5vw, 44px); letter-spacing: -0.01em; margin: 0 0 14px; }
h2 { font-size: 26px; margin: 56px 0 14px; padding-top: 18px; border-top: 1px solid var(--line); }
h3 { font-size: 20px; margin: 34px 0 10px; }
p, ul, ol { margin: 0 0 14px; }
li { margin: 4px 0; }
li > ul, li > ol { margin: 6px 0; }
a { color: var(--accent); }
main hr { display: none; }  /* h2 already draws the section rule */
strong { font-weight: 700; }
code { overflow-wrap: anywhere; font-family: var(--font-mono); font-size: 0.86em; background: var(--inline-code); padding: 1px 5px; border-radius: 4px; }
.codeblock { position: relative; margin: 10px 0 18px; }
pre { margin: 0; background: var(--code-bg); color: var(--code-ink); border-radius: 10px; padding: 16px 18px;
  overflow-x: auto; font-size: 13.5px; line-height: 1.55; }
pre code { background: none; padding: 0; font-size: inherit; color: inherit; overflow-wrap: normal; }
.codeblock pre { padding-top: 38px; }
.copy { position: absolute; top: 8px; right: 8px; font: 600 12px var(--font-body); color: var(--code-ink);
  background: rgb(255 255 255 / 10%); border: 1px solid rgb(255 255 255 / 18%); border-radius: 6px; padding: 4px 9px;
  cursor: pointer; }
.copy:hover { background: rgb(255 255 255 / 18%); }
.table-wrap { overflow-x: auto; margin: 10px 0 20px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-size: 15px; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-family: var(--font-display); font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
tr:last-child td { border-bottom: 0; }

.lede { font-size: 19px; color: var(--muted); }
.runas { display: inline-flex; align-items: center; gap: 10px; margin: 6px 12px 14px 0; padding: 6px 14px 6px 10px;
  border-radius: 999px; font-size: 14px; font-weight: 700; border: 1px solid currentColor; }
.runas::before { content: ""; width: 10px; height: 10px; border-radius: 50%; background: currentColor; }
.runas small { font-weight: 400; color: var(--ink); opacity: 0.8; font-size: 13px; }
.runas--admin { color: var(--admin); background: var(--admin-soft); }
.runas--agents { color: var(--agents); background: var(--agents-soft); }
strong.why { font-family: var(--font-display); font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--accent); background: var(--accent-soft); padding: 2px 8px; border-radius: 4px; margin-right: 4px; }
li.task { list-style: none; margin-left: -22px; display: flex; gap: 10px; align-items: baseline; }
li.task input { width: 17px; height: 17px; accent-color: var(--accent); transform: translateY(2px); flex: none; }
li.task.is-done span { color: var(--muted); text-decoration: line-through; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

@media (max-width: 1000px) {
  .shell { grid-template-columns: minmax(0, 1fr); gap: 0; padding-block: 24px 60px; }
  .toc { position: static; max-height: none; border: 1px solid var(--line); background: var(--surface);
    border-radius: 12px; padding: 14px; margin-bottom: 28px; }
  body { font-size: 16px; }
}
@media (prefers-reduced-motion: no-preference) { html { scroll-behavior: smooth; } }
"""

JS = r"""
(function () {
  // Copy buttons on code blocks
  document.querySelectorAll('pre').forEach(function (pre) {
    var wrap = document.createElement('div'); wrap.className = 'codeblock';
    pre.parentNode.insertBefore(wrap, pre); wrap.appendChild(pre);
    var btn = document.createElement('button'); btn.type = 'button'; btn.className = 'copy'; btn.textContent = 'Copy';
    btn.addEventListener('click', function () {
      var text = pre.innerText;
      var done = function () { btn.textContent = 'Copied'; setTimeout(function () { btn.textContent = 'Copy'; }, 1500); };
      try {
        navigator.clipboard.writeText(text).then(done, function () { select(pre); btn.textContent = 'Press ⌘C'; });
      } catch (e) { select(pre); btn.textContent = 'Press ⌘C'; }
    });
    wrap.appendChild(btn);
  });
  function select(el) { var r = document.createRange(); r.selectNodeContents(el); var s = getSelection(); s.removeAllRanges(); s.addRange(r); }

  // Checklists remembered in this browser only
  var KEY = 'setup-guide-checks';
  var saved = {}; try { saved = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) {}
  document.querySelectorAll('li.task input').forEach(function (box) {
    var id = box.dataset.id; box.checked = !!saved[id];
    box.closest('li').classList.toggle('is-done', box.checked);
    box.addEventListener('change', function () {
      saved[id] = box.checked; box.closest('li').classList.toggle('is-done', box.checked);
      try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) {}
    });
  });

  // Highlight the current section in the contents
  var links = {}; document.querySelectorAll('.toc a').forEach(function (a) { links[a.getAttribute('href').slice(1)] = a; });
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting && links[e.target.id]) {
          Object.values(links).forEach(function (a) { a.classList.remove('is-active'); });
          links[e.target.id].classList.add('is-active');
        }
      });
    }, { rootMargin: '0px 0px -70% 0px' });
    document.querySelectorAll('main h2[id], main h3[id]').forEach(function (h) { io.observe(h); });
  }
})();
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700'
         '&family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700&family=JetBrains+Mono:wght@400;600&display=swap">')

EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]️?\s*")


def postprocess(body: str) -> str:
    # "Run as:" paragraphs -> badges
    def runas(m: re.Match) -> str:
        text = EMOJI.sub("", m.group(1)).strip()
        kind = "admin" if text.lower().startswith("admin") else "agents"
        name, _, rest = text.partition("—")
        rest = rest.strip()
        return (f'<p class="runas runas--{kind}">Run as {name.strip()}'
                + (f' <small>{rest}</small>' if rest else '') + '</p>')
    body = re.sub(r"<p><strong>Run as:</strong>\s*(.*?)</p>", runas, body, flags=re.S)
    # "Why…:" labels
    body = re.sub(r"<strong>(Why[^<]{0,40}?):</strong>", r'<strong class="why">\1</strong>', body)
    # task lists
    counter = iter(range(10_000))
    body = re.sub(r"<li>\[( |x)\]\s*(.*?)</li>",
                  lambda m: (f'<li class="task"><input type="checkbox" id="chk-{(n := next(counter))}" '
                             f'data-id="{n}"{" checked" if m.group(1) == "x" else ""}>'
                             f'<span><label for="chk-{n}">{m.group(2)}</label></span></li>'), body, flags=re.S)
    # tables scroll inside their own box
    body = body.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
    return body


def blank_before_lists(md_text: str) -> str:
    """GitHub starts a list right after a paragraph line; Python-Markdown needs a blank line first."""
    out, fenced, prev = [], False, ""
    for line in md_text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        is_item = bool(re.match(r"\s*([-*]|\d+\.)\s", line))
        prev_is_item = bool(re.match(r"\s*([-*]|\d+\.)\s", prev)) or prev.startswith("  ")
        if not fenced and is_item and prev.strip() and not prev_is_item and not prev.lstrip().startswith("```"):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out) + "\n"


def render(md_text: str, title: str, fragment: bool) -> str:
    md_text = blank_before_lists(md_text)
    md = markdown.Markdown(extensions=["fenced_code", "tables", "sane_lists", "toc"],
                           extension_configs={"toc": {"toc_depth": "2-3", "permalink": False}})
    body = postprocess(md.convert(md_text))
    # first paragraph after h1 = lede
    body = re.sub(r"(</h1>\s*)<p>", r'\1<p class="lede">', body, count=1)
    toc = md.toc.replace('<div class="toc">', '<nav class="toc" aria-label="Contents"><p class="toc__title">Contents</p>')
    toc = toc.replace("</div>", "</nav>")
    page = (f'<title>{html.escape(title)}</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n{FONTS}\n'
            f'<style>{CSS}</style>\n<div class="shell">{toc}<main>{body}</main></div>\n<script>{JS}</script>\n')
    if fragment:
        return page
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            f'{page.split("<div class=\"shell\">")[0]}</head>\n<body>\n<div class="shell">'
            f'{page.split("<div class=\"shell\">", 1)[1]}</body>\n</html>\n')


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", nargs="?", default=str(ROOT / "docs" / "HERMES_MAC_MINI_SETUP.md"))
    p.add_argument("-o", "--out")
    p.add_argument("--title", default="Mac Mini Agent Server")
    p.add_argument("--fragment", action="store_true", help="omit the <html>/<head>/<body> wrapper")
    a = p.parse_args()
    src = Path(a.source)
    out = Path(a.out) if a.out else src.with_suffix(".html")
    out.write_text(render(src.read_text(encoding="utf-8"), a.title, a.fragment), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
