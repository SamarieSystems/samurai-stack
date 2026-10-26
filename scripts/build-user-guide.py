#!/usr/bin/env python3
"""Build an offline, printable user guide using only the Python standard library.

This reads documentation, writes a new output directory outside the repository,
and never runs installer or service commands. It does not publish anything.
"""
from __future__ import annotations

import argparse
import hashlib
from html import escape, unescape
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit
import zipfile


GUIDE_PAGES = tuple('docs/guide/' + name + '.md' for name in (
    'README', 'START-HERE', 'DAILY-USE', 'MEDIA-AND-DOWNLOADS', 'INSTALL-A-NEW-SERVER',
    'OPERATING-THE-SERVER', 'TROUBLESHOOTING', 'BACKUPS-AND-UPDATES',
    'HANDOVER-CHECKLIST', 'GLOSSARY',
))
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
HEADING = re.compile(r'^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$')
LIST_ITEM = re.compile(r'^( *)([-+*]|\d+[.)])\s+(.*)$')
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})([^`]*)$')
TOKEN = re.compile(
    r'(`+)(.+?)\1|(!?)\[([^\]\n]*)\]\(([^\s)]*)(?:\s+"[^"]*")?\)'
    r'|<(https?://[^<>\s]+|mailto:[^<>\s]+)>'
    r'|\*\*(.+?)\*\*|__(.+?)__|~~(.+?)~~|(?<!\*)\*([^*\n]+)\*(?!\*)'
    r'|(?<!\\)\\([\\`*_{}\[\]()#+.!|>-])'
)


class GuideError(RuntimeError):
    """Documentation cannot be packaged without losing content or links."""


def page_id(path: str) -> str:
    return 'page-' + re.sub(r'[^a-z0-9]+', '-', path.lower()).strip('-')


def heading_slug(text: str) -> str:
    text = re.sub(r'!?\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = unescape(re.sub(r'[`*_~]', '', text)).strip().lower()
    text = re.sub(r'[^\w\- ]', '', text, flags=re.UNICODE)
    return re.sub(r'\s', '-', text)


def _headings(text: str) -> dict[int, tuple[int, str, str]]:
    headings = {}
    counts: dict[str, int] = {}
    fence = None
    for index, line in enumerate(text.splitlines()):
        opening = FENCE.match(line)
        if opening:
            marker = opening.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        match = HEADING.match(line)
        if match:
            level, title = len(match.group(1)), match.group(2)
            slug = heading_slug(title)
            number = counts.get(slug, 0)
            counts[slug] = number + 1
            headings[index] = (level, title, slug + ('-' + str(number) if number else ''))
    if fence:
        raise GuideError('A document has an unclosed fenced code block.')
    return headings


def collect_documents(root: Path) -> dict[str, str]:
    """Read the public guide allowlist and top-level technical Markdown."""
    root = root.resolve()
    paths = {root / name for name in GUIDE_PAGES}
    paths.update((root / 'docs').glob('*.md'))
    paths.update(root / name for name in ('README.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'LICENSE'))
    documents = {}
    total = 0
    for path in sorted(paths):
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise GuideError('Required public documentation is missing or unsafe: ' + path.name)
        data = path.read_bytes()
        if len(data) > MAX_DOCUMENT_BYTES:
            raise GuideError('A documentation file exceeds the size limit: ' + path.name)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise GuideError('Documentation exceeds the total size limit.')
        documents[path.relative_to(root).as_posix()] = data.decode('utf-8-sig')
    return documents


class Renderer:
    def __init__(self, documents: dict[str, str]):
        self.documents = documents
        self.headings = {path: _headings(text) for path, text in documents.items() if path.endswith('.md')}
        self.links = 0
        self.external_links = 0
        self.code_blocks = 0
        self.issues: list[str] = []

    def href(self, source: str, raw: str) -> tuple[str, bool]:
        url = unescape(raw)
        if any(ord(char) < 32 for char in url) or '\\' in url:
            self.issues.append(source + ': unsafe link')
            return '#', False
        parts = urlsplit(url)
        if parts.scheme or parts.netloc:
            if parts.scheme.lower() not in {'http', 'https', 'mailto'} or (parts.netloc and not parts.scheme):
                self.issues.append(source + ': unsupported link scheme')
                return '#', False
            self.external_links += 1
            return url, True
        target = posixpath.normpath(posixpath.join(posixpath.dirname(source), unquote(parts.path))) if parts.path else source
        if target not in self.documents:
            self.issues.append(source + ': local link target is not bundled: ' + target)
            return '#', False
        result = page_id(target)
        if parts.fragment:
            fragment = unquote(parts.fragment)
            slugs = {heading[2] for heading in self.headings.get(target, {}).values()}
            if fragment not in slugs:
                self.issues.append(source + ': missing heading: ' + target + '#' + fragment)
                return '#', False
            result += '--' + fragment
        self.links += 1
        return '#' + result, False

    def inline(self, text: str, source: str, depth: int = 0) -> str:
        if depth > 8:
            return escape(text)
        # A linked badge becomes an ordinary text link, never a remote image.
        text = re.sub(r'\[!\[([^\]]*)\]\([^)]*\)\]\(([^)]*)\)', r'[\1](\2)', text)
        out, cursor = [], 0
        for match in TOKEN.finditer(text):
            out.append(escape(text[cursor:match.start()]))
            groups = match.groups()
            if groups[0] is not None:
                out.append('<code>' + escape(groups[1].strip()) + '</code>')
            elif groups[2] is not None:
                image, label, url = groups[2:5]
                if image:
                    out.append('<span class="image-label">' + escape(label or 'Image') + '</span>')
                else:
                    href, external = self.href(source, url)
                    extra = ' rel="noreferrer noopener" class="external"' if external else ''
                    out.append('<a href="' + escape(href, quote=True) + '"' + extra + '>' + self.inline(label, source, depth + 1) + '</a>')
            elif groups[5] is not None:
                href, external = self.href(source, groups[5])
                out.append('<a href="' + escape(href, quote=True) + '" rel="noreferrer noopener" class="external">' + escape(groups[5]) + '</a>')
            elif groups[6] is not None or groups[7] is not None:
                out.append('<strong>' + self.inline(groups[6] or groups[7], source, depth + 1) + '</strong>')
            elif groups[8] is not None:
                out.append('<del>' + self.inline(groups[8], source, depth + 1) + '</del>')
            elif groups[9] is not None:
                out.append('<em>' + self.inline(groups[9], source, depth + 1) + '</em>')
            else:
                out.append(escape(groups[10]))
            cursor = match.end()
        out.append(escape(text[cursor:]))
        return ''.join(out)

    @staticmethod
    def cells(line: str) -> list[str]:
        parts, text, quoted, escaped = [], '', False, False
        for char in line.strip().strip('|'):
            if escaped:
                text += '\\' + char
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '`':
                quoted = not quoted
                text += char
            elif char == '|' and not quoted:
                parts.append(text.strip())
                text = ''
            else:
                text += char
        parts.append(text.strip())
        return parts

    def blocks(self, lines: list[str], source: str, start: int = 0, heading_map=None) -> str:
        out, index = [], 0
        heading_map = heading_map or {}
        while index < len(lines):
            line = lines[index]
            if not line.strip():
                index += 1
                continue
            opening = FENCE.match(line)
            if opening:
                marker, language = opening.group(1), opening.group(2).strip()
                body = []
                index += 1
                while index < len(lines):
                    close = FENCE.match(lines[index])
                    if close and close.group(1)[0] == marker[0] and len(close.group(1)) >= len(marker):
                        break
                    body.append(lines[index])
                    index += 1
                if index == len(lines):
                    raise GuideError('Unclosed code block in ' + source)
                self.code_blocks += 1
                label = '<span class="code-language">' + escape(language) + '</span>' if language else ''
                out.append('<div class="code-block">' + label + '<pre tabindex="0"><code>' + escape('\n'.join(body)) + '</code></pre></div>')
                index += 1
                continue
            if start + index in heading_map:
                level, title, slug = heading_map[start + index]
                level = min(level + 1, 6)
                out.append(f'<h{level} id="{escape(page_id(source) + "--" + slug, quote=True)}">' + self.inline(title, source) + f'</h{level}>')
                index += 1
                continue
            if re.fullmatch(r'\s*(?:\*\s*){3,}|\s*(?:-\s*){3,}|\s*(?:_\s*){3,}', line):
                out.append('<hr>')
                index += 1
                continue
            if index + 1 < len(lines) and '|' in line:
                delimiters = self.cells(lines[index + 1])
                if delimiters and all(re.fullmatch(r':?-{3,}:?', cell) for cell in delimiters):
                    header = self.cells(line)
                    if len(header) != len(delimiters):
                        raise GuideError('Malformed table in ' + source)
                    index += 2
                    rows = []
                    while index < len(lines) and lines[index].strip().startswith('|'):
                        cells = self.cells(lines[index])
                        if len(cells) != len(header):
                            raise GuideError('Table row width differs in ' + source)
                        rows.append('<tr>' + ''.join('<td>' + self.inline(cell, source) + '</td>' for cell in cells) + '</tr>')
                        index += 1
                    out.append('<div class="table-wrap" tabindex="0"><table><thead><tr>' + ''.join('<th scope="col">' + self.inline(cell, source) + '</th>' for cell in header) + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')
                    continue
            if line.lstrip().startswith('>'):
                body = []
                while index < len(lines) and lines[index].lstrip().startswith('>'):
                    body.append(re.sub(r'^\s*> ?', '', lines[index]))
                    index += 1
                out.append('<blockquote>' + self.blocks(body, source) + '</blockquote>')
                continue
            item = LIST_ITEM.match(line)
            if item:
                base, ordered = len(item.group(1)), item.group(2)[0].isdigit()
                tag = 'ol' if ordered else 'ul'
                number = re.sub(r'\D', '', item.group(2))
                attr = ' start="' + number + '"' if ordered and number != '1' else ''
                items = []
                while index < len(lines):
                    match = LIST_ITEM.match(lines[index])
                    if not match or len(match.group(1)) != base or match.group(2)[0].isdigit() != ordered:
                        break
                    content_indent = match.start(3)
                    body = [match.group(3)]
                    index += 1
                    while index < len(lines):
                        following = LIST_ITEM.match(lines[index])
                        if following and len(following.group(1)) <= base:
                            break
                        if not lines[index].strip():
                            if index + 1 >= len(lines) or (lines[index + 1] and not lines[index + 1].startswith(' ' * (base + 1))):
                                break
                            body.append('')
                        elif len(lines[index]) - len(lines[index].lstrip()) > base:
                            trim = min(content_indent, len(lines[index]) - len(lines[index].lstrip()))
                            body.append(lines[index][trim:])
                        else:
                            if HEADING.match(lines[index]) or FENCE.match(lines[index]) or lines[index].startswith('|'):
                                break
                            body.append(lines[index])
                        index += 1
                    items.append('<li>' + self.blocks(body, source) + '</li>')
                out.append('<' + tag + attr + '>' + ''.join(items) + '</' + tag + '>')
                continue
            body = [line.strip()]
            index += 1
            while index < len(lines) and lines[index].strip():
                candidate = lines[index]
                if HEADING.match(candidate) or FENCE.match(candidate) or LIST_ITEM.match(candidate) or candidate.lstrip().startswith('>'):
                    break
                if index + 1 < len(lines) and '|' in candidate and all(re.fullmatch(r':?-{3,}:?', cell) for cell in self.cells(lines[index + 1])):
                    break
                body.append(candidate.strip())
                index += 1
            out.append('<p>' + self.inline(' '.join(body), source) + '</p>')
        return '\n'.join(out)

    def document(self, path: str) -> str:
        if not path.endswith('.md'):
            return '<h2>License</h2><pre class="license">' + escape(self.documents[path]) + '</pre>'
        return self.blocks(self.documents[path].splitlines(), path, heading_map=self.headings[path])


CSS = r'''
:root{color-scheme:light;--ink:#172334;--muted:#526274;--line:#dce4eb;--accent:#086d68;--paper:#fff;--wash:#f4f7fa;--nav:#122839}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:1rem}body{margin:0;background:var(--wash);color:var(--ink);font:16px/1.65 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:#065f8b;text-underline-offset:.18em}a:hover{color:var(--accent)}a:focus-visible,button:focus-visible,input:focus-visible,summary:focus-visible,[tabindex]:focus-visible{outline:3px solid #d68d17;outline-offset:3px}.skip{position:absolute;top:-5em;left:1rem;background:white;padding:.5rem;z-index:20}.skip:focus{top:1rem}
.sidebar{position:fixed;inset:0 auto 0 0;width:292px;background:var(--nav);color:#edf6f7;padding:1.5rem;overflow:auto}.brand{font-size:1.5rem;font-weight:750;letter-spacing:-.03em;margin:0}.version{color:#9ed3ce;font-size:.85rem;margin:.2rem 0 1.4rem}.sidebar label{font-size:.86rem;font-weight:650;display:block}.search-row{display:flex;gap:.35rem;margin-top:.4rem}.search-row input{min-width:0;flex:1;padding:.65rem;border:1px solid #607588;border-radius:6px;background:#fff;color:var(--ink);font:inherit}.search-row button{border:1px solid #6c8497;background:transparent;color:white;border-radius:6px;padding:.4rem .6rem;cursor:pointer}.search-hint,.search-status{font-size:.78rem;color:#c0d0dc;line-height:1.45;margin:.55rem 0}.nav-heading{font-size:.76rem;text-transform:uppercase;letter-spacing:.1em;color:#9ed3ce;margin:1.5rem 0 .4rem}.sidebar nav a{display:block;color:#e0eaf1;text-decoration:none;padding:.42rem .55rem;margin:.08rem 0;border-radius:5px;font-size:.9rem;line-height:1.4}.sidebar nav a:hover,.sidebar nav a[aria-current]{background:#23465b;color:#fff}.sidebar details summary{cursor:pointer;margin-top:1rem;color:#9ed3ce;font-size:.86rem}.sidebar footer{border-top:1px solid #365265;margin-top:1.4rem;padding-top:1rem;font-size:.78rem;color:#bdcdda}.sidebar footer button{border:1px solid #73909f;border-radius:6px;background:transparent;padding:.45rem .65rem;color:#fff;cursor:pointer}
main{margin-left:292px;max-width:1360px;padding:2.8rem clamp(1.3rem,4vw,4rem)}.hero{background:linear-gradient(130deg,#e1f3ef,#eaf2fa);border:1px solid #c6dedb;padding:2rem;border-radius:14px;margin-bottom:2rem}.hero h1{font-size:clamp(1.9rem,4vw,2.8rem);line-height:1.15;letter-spacing:-.045em;margin:0 0 1rem}.hero p{margin:.4rem 0;max-width:72ch}.eyebrow{font-size:.78rem;font-weight:750;text-transform:uppercase;letter-spacing:.08em;color:#276b67}.document{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:clamp(1.2rem,3vw,2.5rem);margin:0 0 1.8rem;overflow-wrap:anywhere}.document h2{font-size:1.9rem;line-height:1.25;margin:.1rem 0 1rem;letter-spacing:-.03em}.document h3{font-size:1.35rem;line-height:1.35;margin:2rem 0 .6rem;letter-spacing:-.015em}.document h4{font-size:1.1rem;margin:1.5rem 0 .5rem}.document h5,.document h6{font-size:1rem;margin:1.2rem 0 .3rem}.document p{margin:.6rem 0 1rem}.document ul,.document ol{padding-left:1.55rem}.document li{padding-left:.2rem;margin:.3rem 0}.document li p{margin:.3rem 0 .5rem}.source-path{font:12px/1.5 ui-monospace,Consolas,monospace;color:var(--muted);margin-top:2rem;padding-top:1rem;border-top:1px solid var(--line)}code{font: .88em/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;background:#edf2f6;color:#253e56;border-radius:4px;padding:.1em .3em}pre{background:#132a3c;color:#edf5fb;border-radius:8px;padding:1.1rem;overflow:auto;line-height:1.55;tab-size:4;white-space:pre}pre code{background:none;color:inherit;padding:0;font-size:.82rem}.code-block{position:relative}.code-language{display:block;margin-top:1rem;font-size:.74rem;font-weight:650;color:var(--muted)}.code-language+pre{margin-top:.3rem}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px;margin:1rem 0 1.5rem}table{border-collapse:collapse;width:100%;font-size:.91rem;line-height:1.5}th,td{text-align:left;vertical-align:top;padding:.7rem .8rem;border-bottom:1px solid var(--line)}th{background:#edf5f6;font-weight:700}tr:last-child td{border-bottom:0}blockquote{margin:1rem 0;border-left:4px solid #5b9f96;background:#eff8f5;padding:.3rem 1.1rem;color:#284f4d}hr{border:0;border-top:1px solid var(--line);margin:1.5rem 0}.external:after{content:" [web]";font-size:.72em;color:var(--muted)}.image-label{font-size:.82rem;color:var(--muted)}.license{white-space:pre-wrap;background:#f3f5f7;color:var(--ink);font-size:.75rem}.empty{padding:2rem;background:#fff2cd;border:1px solid #ebd79f;border-radius:10px}body [hidden]{display:none!important}
@media(max-width:860px){.sidebar{position:relative;width:auto;padding:1.2rem}.sidebar nav{columns:2}.sidebar nav a{break-inside:avoid}.sidebar details,.sidebar footer{column-span:all}.sidebar nav details a{break-inside:avoid}main{margin-left:0;padding:1.2rem}.hero{padding:1.4rem}.sidebar nav .nav-heading{column-span:all}.search-row{max-width:500px}}@media(max-width:480px){.sidebar nav{columns:1}.document{border-radius:8px;padding:1.1rem}.document h2{font-size:1.55rem}.hero h1{font-size:2rem}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
@media print{@page{size:A4;margin:16mm 14mm}html{scroll-behavior:auto}body{background:#fff;color:#000;font:10pt/1.45 sans-serif}.sidebar,.skip,.print-controls,.empty{display:none!important}main{margin:0;padding:0;max-width:none}.hero{background:#fff;border:0;padding:0 0 10mm;border-bottom:2px solid #222;border-radius:0}.hero h1{font-size:25pt}.document,body .document[hidden]{display:block!important;border:0;border-radius:0;padding:0;margin:0;break-before:page}.document h2{font-size:20pt}.document h3{font-size:14pt}.document h2,.document h3,.document h4{break-after:avoid}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f2f2;color:#000;font-size:8pt;border:1px solid #ccc;padding:3mm}pre code{font-size:8pt;color:#000}.table-wrap{overflow:visible;border-radius:0}table{font-size:8.5pt}thead{display:table-header-group}tr{break-inside:avoid}th,td{padding:2mm;border-color:#bbb}a{color:#000;text-decoration:underline}.external:after{content:" (online)"}.source-path{font-size:8pt}.hero p{max-width:none}}
'''

JS = r'''
(()=>{"use strict";
const input=document.getElementById("guide-search"),clear=document.getElementById("clear-search"),status=document.getElementById("search-status"),empty=document.getElementById("no-results");
const pages=[...document.querySelectorAll("section[data-document]")],links=[...document.querySelectorAll("nav a[data-page]")];
const indexed=pages.map(page=>({page,text:page.textContent.toLowerCase(),id:page.id}));
function filter(){const words=input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);let visible=0;for(const row of indexed){const show=words.every(word=>row.text.includes(word));row.page.hidden=!show;if(show)visible++;const link=links.find(item=>item.dataset.page===row.id);if(link)link.hidden=!show;}empty.hidden=visible!==0;status.textContent=words.length?visible+" of "+pages.length+" chapters match. Search covers the whole manual.":"Search the guide and technical reference. Ctrl+K focuses search.";}
input.addEventListener("input",filter);clear.addEventListener("click",()=>{input.value="";filter();input.focus();});
document.addEventListener("keydown",event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==="k"){event.preventDefault();input.focus();input.select();}if(event.key==="Escape"&&document.activeElement===input){input.value="";filter();}});
function revealHash(){const id=decodeURIComponent(location.hash.slice(1));if(!id)return;const target=document.getElementById(id);if(!target)return;const page=target.closest("section[data-document]");if(page&&page.hidden){input.value="";filter();}for(const link of links){if(page&&link.dataset.page===page.id)link.setAttribute("aria-current","location");else link.removeAttribute("aria-current");}if(page){const link=links.find(item=>item.dataset.page===page.id);const details=link&&link.closest("details");if(details)details.open=true;}requestAnimationFrame(()=>target.scrollIntoView({block:"start"}));}
window.addEventListener("hashchange",revealHash);
document.getElementById("print-guide").addEventListener("click",()=>{document.body.dataset.printScope="guide";window.print();});
document.getElementById("print-all").addEventListener("click",()=>{document.body.dataset.printScope="all";window.print();});
window.addEventListener("afterprint",()=>{document.body.dataset.printScope="guide";});
filter();revealHash();
})();
'''


class LinkCheck(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.anchors, self.resource_urls = set(), [], []
        self.duplicates = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if 'id' in values:
            if values['id'] in self.ids:
                self.duplicates.append(values['id'])
            self.ids.add(values['id'])
        if tag == 'a' and 'href' in values and values['href'].startswith('#'):
            self.anchors.append(values['href'][1:])
        if 'src' in values or (tag == 'link' and 'href' in values):
            self.resource_urls.append(values.get('src', values.get('href')))


def build_html(documents: dict[str, str], version: str, *, title: str = 'Samurai Stack',
               guide_paths: tuple[str, ...] = GUIDE_PAGES) -> tuple[str, dict]:
    """Render explicit document bytes; callers may add a separate private page.

    This function neither reads private files nor writes output. The public CLI
    supplies only collect_documents()'s public documentation allowlist.
    """
    for path in documents:
        if path != str(PurePosixPath(path)) or path.startswith('/') or '..' in PurePosixPath(path).parts:
            raise GuideError('An input document name is not a safe relative POSIX path.')
    if len({page_id(path) for path in documents}) != len(documents):
        raise GuideError('Document page identifiers collide.')
    for path in guide_paths:
        if path not in documents:
            raise GuideError('Missing guide page: ' + path)
    renderer = Renderer(documents)
    ordered = list(guide_paths) + sorted(set(documents) - set(guide_paths))
    navigation, articles = [], []
    for path in ordered:
        headings = renderer.headings.get(path, {})
        label = next(iter(headings.values()))[1] if headings else 'License'
        label = re.sub(r'[`*_~]', '', label)
        nav = '<a href="#' + page_id(path) + '" data-page="' + page_id(path) + '">' + escape(label) + '</a>'
        navigation.append(nav)
        reference = 'false' if path in guide_paths else 'true'
        articles.append('<section class="document" id="' + page_id(path) + '" data-reference="' + reference + '" data-document="' + escape(path, quote=True) + '">' + renderer.document(path) + '<div class="source-path">Source: ' + escape(path) + '</div></section>')
    if renderer.issues:
        raise GuideError('Documentation link check failed:\n' + '\n'.join(sorted(set(renderer.issues))))
    count = len(guide_paths)
    html = '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' \
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; img-src data:; base-uri \'none\'; form-action \'none\'">' \
        '<meta name="description" content="Offline, searchable and printable Samurai Stack user guide."><title>' + escape(title) + ' User Guide</title><style>' + CSS + '@media print{body[data-print-scope="guide"] .document[data-reference="true"]{display:none!important}}</style></head><body data-print-scope="guide">' \
        '<a class="skip" href="#content">Skip to content</a><aside class="sidebar"><p class="brand">' + escape(title) + '</p><p class="version">User guide | version ' + escape(version) + '</p>' \
        '<label for="guide-search">Find a task or problem</label><div class="search-row"><input id="guide-search" type="search" placeholder="Try subtitles, storage, VPN..." autocomplete="off" aria-describedby="search-status"><button id="clear-search" type="button" aria-label="Clear search">Clear</button></div>' \
        '<p id="search-status" class="search-status" role="status" aria-live="polite"></p><nav aria-label="Guide chapters"><p class="nav-heading">Use your server</p>' + ''.join(navigation[:count]) + \
        '<details><summary>Technical reference</summary>' + ''.join(navigation[count:]) + '</details></nav>' \
        '<footer><p>One file. Works offline. Online source links are marked [web].</p><button id="print-guide" type="button">Print user guide / PDF</button><p><button id="print-all" type="button">Print complete manual</button></p><p>Normal printing includes the user guide. The complete manual also includes the technical reference.</p><p>This pack is documentation, not an installer.</p></footer></aside>' \
        '<main id="content"><header class="hero"><p class="eyebrow">From first request to running the server</p><h1>Your server, explained.</h1><p>Start with the everyday guide. Open the technical reference when you need the exact settings or recovery procedure.</p><p>Use the search box to find a task. Print the user guide or choose the complete manual with its technical reference. Local chapter links work without internet; provider websites and online sources still need a connection.</p></header>' \
        '<div id="no-results" class="empty" hidden>No chapters match. Try a shorter term, or clear the search.</div>' + '\n'.join(articles) + '</main><script>' + JS + '</script></body></html>\n'
    checker = LinkCheck()
    checker.feed(html)
    missing = sorted(set(checker.anchors) - checker.ids)
    if checker.duplicates or missing or checker.resource_urls:
        raise GuideError('Generated HTML failed anchor/resource validation: ' + repr({'duplicate_ids': checker.duplicates, 'missing': missing, 'resources': checker.resource_urls}))
    stats = {'documents': len(documents), 'guide_chapters': count, 'internal_links': renderer.links,
             'external_links': renderer.external_links, 'code_blocks': renderer.code_blocks,
             'html_ids': len(checker.ids), 'broken_internal_links': 0, 'network_resources': 0}
    return html, stats


def _zip_write(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def build(root: Path, output: Path) -> dict:
    root, output = root.resolve(), output.resolve()
    if output == root or output.is_relative_to(root):
        raise GuideError('Choose a new output directory outside the source repository.')
    if output.exists():
        raise GuideError('Output already exists. Choose a new directory; nothing was overwritten.')
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', (root / 'samurai_stack/__init__.py').read_text(), re.MULTILINE)
    if match is None or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){2}(?:(?:a|b|rc)[0-9]+)?', match.group(1)):
        raise GuideError('The source version could not be identified.')
    version = match.group(1)
    documents = collect_documents(root)
    html, stats = build_html(documents, version)
    try:
        commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL, text=True).strip()
        dirty = bool(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], stderr=subprocess.DEVNULL))
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    manifest = {'format': 1, 'product': 'Samurai Stack user documentation', 'version': version,
                'source_commit': commit, 'worktree_dirty': dirty, 'validation': stats,
                'documents': {path: hashlib.sha256(text.encode('utf-8')).hexdigest() for path, text in sorted(documents.items())}}
    output.mkdir(parents=True)
    stem = 'samurai-stack-v' + version + '-user-guide'
    html_path = output / (stem + '.html')
    html_path.write_bytes(html.encode('utf-8'))
    archive_path = output / (stem + '.zip')
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode('utf-8')
    with zipfile.ZipFile(archive_path, 'x') as archive:
        _zip_write(archive, 'index.html', html.encode('utf-8'))
        _zip_write(archive, 'DOCUMENTATION-MANIFEST.json', manifest_bytes)
        _zip_write(archive, 'OPEN-ME.txt', b'Open index.html in a web browser. No installation or internet connection is needed for the manual.\nUse Print / Save PDF for a printable copy. Files under source/ are the same Markdown documentation.\nThis is a documentation pack, not the server installer. Private server addresses and passwords are not supplied.\n')
        for path, text in sorted(documents.items()):
            _zip_write(archive, 'source/' + path, text.encode('utf-8'))
    (output / 'DOCUMENTATION-MANIFEST.json').write_bytes(manifest_bytes)
    checksums = ''.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n'
                        for path in (html_path, archive_path, output / 'DOCUMENTATION-MANIFEST.json'))
    (output / 'USER-GUIDE-SHA256SUMS').write_text(checksums, encoding='ascii', newline='\n')
    return {'version': version, 'html': str(html_path), 'zip': str(archive_path), 'validation': stats,
            'source_commit': commit, 'worktree_dirty': dirty}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1], help='Source checkout root (defaults to this script repository).')
    parser.add_argument('--output', type=Path, required=True, help='New output directory outside the source checkout.')
    args = parser.parse_args()
    try:
        report = build(args.source, args.output)
    except (GuideError, OSError, UnicodeError, ValueError) as exc:
        print('User guide build refused: ' + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
