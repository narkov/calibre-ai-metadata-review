from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from core import (
    AuthorRegistry,
    Suggestion,
    ai_prompt_payload,
    local_suggestion,
    normalize_text,
    pretty_authors,
    split_author_blob,
)
from openai_client import call_openai_responses, resolve_api_key


DEFAULT_DB = Path('/opt/calibre/library/metadata.db')
DEFAULT_LIBRARY_ROOT = Path('/opt/calibre/library')
DEFAULT_BACKUP_ROOT = Path('/opt/calibre/backups/ai-metadata-review')
DEFAULT_SETTINGS_PATH = Path('/opt/calibre/config/ai_metadata_review_web.json')


def title_sort_func(title: str) -> str:
    if not title:
        return ''
    match = re.match(r'^(A|An|The)\s+(.+)', title)
    return f'{match.group(2)}, {match.group(1)}' if match else title


def author_sort_value(name: str) -> str:
    name = name.strip()
    if ',' in name:
        return name
    parts = name.split()
    if len(parts) >= 2:
        return f"{parts[-1]}, {' '.join(parts[:-1])}"
    return name


def update_opf(path: Path, new_title: str, new_authors: list[str], backup_root: Path, library_root: Path) -> bool:
    if not path.exists():
        return False
    backup_path = backup_root / path.relative_to(library_root)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_path)

    ns_dc = 'http://purl.org/dc/elements/1.1/'
    ns_opf = 'http://www.idpf.org/2007/opf'
    ET.register_namespace('dc', ns_dc)
    ET.register_namespace('opf', ns_opf)

    tree = ET.parse(path)
    root = tree.getroot()
    metadata = root.find(f'{{{ns_opf}}}metadata')
    if metadata is None:
        return False

    title_el = metadata.find(f'{{{ns_dc}}}title')
    if title_el is None:
        title_el = ET.SubElement(metadata, f'{{{ns_dc}}}title')
    title_el.text = new_title

    sort_el = None
    for meta in metadata.findall(f'{{{ns_opf}}}meta'):
        if meta.get('name') == 'calibre:title_sort':
            sort_el = meta
            break
    if sort_el is None:
        sort_el = ET.SubElement(metadata, f'{{{ns_opf}}}meta')
        sort_el.set('name', 'calibre:title_sort')
    sort_el.set('content', title_sort_func(new_title))

    for creator in list(metadata.findall(f'{{{ns_dc}}}creator')):
        metadata.remove(creator)
    for creator in list(metadata.findall(f'{{{ns_opf}}}creator')):
        metadata.remove(creator)

    insert_at = 0
    children = list(metadata)
    for idx, child in enumerate(children):
        if child.tag == f'{{{ns_dc}}}date':
            insert_at = idx
            break

    for offset, author in enumerate([a.strip() for a in new_authors if a.strip()]):
        creator = ET.Element(f'{{{ns_dc}}}creator')
        creator.text = author
        creator.set(f'{{{ns_opf}}}role', 'aut')
        creator.set(f'{{{ns_opf}}}file-as', author_sort_value(author))
        metadata.insert(insert_at + offset, creator)

    tree.write(path, encoding='utf-8', xml_declaration=True)
    return True


@dataclass
class BookRow:
    book_id: int
    title: str
    authors: list[str]
    path: str
    suggestion: Suggestion | None = None
    suspicious: bool = False
    source: str = 'none'
    reason: str = ''


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {
            'db_path': str(DEFAULT_DB),
            'library_root': str(DEFAULT_LIBRARY_ROOT),
            'backup_root': str(DEFAULT_BACKUP_ROOT),
            'openai_api_key': '',
            'openai_base_url': 'https://api.openai.com/v1',
            'openai_model': 'gpt-5.4-mini',
            'use_openai': True,
            'max_rows': 200,
        }
        self.load()

    def load(self):
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding='utf-8')))
            except Exception:
                pass

    def save(self):
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')


class CalibreReviewDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.create_function('title_sort', 1, title_sort_func)
        return con

    def get_book(self, book_id: int) -> BookRow | None:
        con = self.connect()
        try:
            row = con.execute('SELECT id, title, path FROM books WHERE id = ?', (book_id,)).fetchone()
            if not row:
                return None
            authors = [
                r['name']
                for r in con.execute(
                    'SELECT a.name FROM authors a JOIN books_authors_link bal ON bal.author = a.id WHERE bal.book = ? ORDER BY bal.id',
                    (book_id,),
                )
            ]
            if not authors:
                authors = ['Unknown']
            return BookRow(book_id=int(row['id']), title=row['title'] or '', authors=authors, path=row['path'] or '')
        finally:
            con.close()

    def list_candidates(self, registry: AuthorRegistry, limit: int = 200) -> list[BookRow]:
        con = self.connect()
        rows: list[BookRow] = []
        try:
            cur = con.execute(
                'SELECT id, title, path FROM books ORDER BY id DESC LIMIT ?',
                (limit,),
            )
            for row in cur:
                book_id = int(row['id'])
                authors = [
                    r['name']
                    for r in con.execute(
                        'SELECT a.name FROM authors a JOIN books_authors_link bal ON bal.author = a.id WHERE bal.book = ? ORDER BY bal.id',
                        (book_id,),
                    )
                ]
                if not authors:
                    authors = ['Unknown']
                candidate = BookRow(book_id=book_id, title=row['title'] or '', authors=authors, path=row['path'] or '')
                suggestion = local_suggestion(candidate.title, candidate.authors, registry)
                suspicious = suggestion is not None and suggestion.action == 'update'
                if not suspicious and self._looks_suspicious(candidate.title, candidate.authors, registry):
                    suspicious = True
                    suggestion = suggestion or Suggestion(
                        title=candidate.title,
                        authors=candidate.authors,
                        source='heuristic',
                        confidence=0.2,
                        reason='Manual review recommended.',
                        action='leave',
                        raw={},
                    )
                candidate.suggestion = suggestion
                candidate.suspicious = suspicious
                if suggestion:
                    candidate.source = suggestion.source
                    candidate.reason = suggestion.reason
                rows.append(candidate)
            return rows
        finally:
            con.close()

    def _looks_suspicious(self, title: str, authors: list[str], registry: AuthorRegistry) -> bool:
        if any(registry.is_junk(a) for a in authors):
            return True
        if len(authors) == 1 and not registry.is_valid_single_token(authors[0]):
            if len(authors[0].split()) == 1:
                return True
        title_norm = normalize_text(title)
        if title_norm.startswith('unknown') or title_norm.startswith('windows') or title_norm.startswith('chapter'):
            return True
        if re.search(r'\b(version|user|thriller|analysis|guide|book|stories)\b', title, re.I):
            return True
        return False

    def apply_rows(
        self,
        rows: list[BookRow],
        registry: AuthorRegistry,
        backup_root: Path,
        library_root: Path,
    ) -> int:
        if not rows:
            return 0
        backup_root.mkdir(parents=True, exist_ok=True)
        con = self.connect()
        changed = 0
        try:
            cur = con.cursor()
            cur.execute('BEGIN IMMEDIATE')
            timestamp = time.strftime('%Y%m%d-%H%M%S')
            db_backup = backup_root / f'metadata-{timestamp}.db'
            shutil.copy2(self.db_path, db_backup)
            opf_root = backup_root / f'opf-{timestamp}'
            opf_root.mkdir(parents=True, exist_ok=True)
            for row in rows:
                suggestion = row.suggestion
                if not suggestion or suggestion.action != 'update':
                    continue
                db_row = cur.execute('SELECT id, title, path FROM books WHERE id = ?', (row.book_id,)).fetchone()
                if not db_row:
                    continue
                current_authors = [
                    r['name']
                    for r in cur.execute(
                        'SELECT a.name FROM authors a JOIN books_authors_link bal ON bal.author = a.id WHERE bal.book = ? ORDER BY bal.id',
                        (row.book_id,),
                    )
                ]
                new_title = suggestion.title.strip() if suggestion.title else db_row['title'] or ''
                new_authors = [a.strip() for a in suggestion.authors if a.strip()] or ['Unknown']
                if new_authors and new_authors != current_authors or new_title != (db_row['title'] or ''):
                    cur.execute('DELETE FROM books_authors_link WHERE book = ?', (row.book_id,))
                    author_ids = [self.get_or_create_author(cur, name) for name in new_authors]
                    for author_id in author_ids:
                        cur.execute(
                            'INSERT OR IGNORE INTO books_authors_link(book, author) VALUES (?, ?)',
                            (row.book_id, author_id),
                        )
                    cur.execute(
                        'UPDATE books SET title = ?, author_sort = ?, sort = title_sort(?), last_modified = datetime(\'now\') WHERE id = ?',
                        (new_title, ' | '.join(new_authors), new_title, row.book_id),
                    )
                    cur.execute('INSERT OR IGNORE INTO metadata_dirtied(book) VALUES (?)', (row.book_id,))
                    changed += 1
                if db_row['path']:
                    update_opf(
                        library_root / db_row['path'] / 'metadata.opf',
                        new_title,
                        new_authors,
                        opf_root,
                        library_root,
                    )
            con.commit()
            return changed
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def get_or_create_author(self, cur: sqlite3.Cursor, name: str) -> int:
        row = cur.execute('SELECT id FROM authors WHERE name = ?', (name,)).fetchone()
        if row:
            return int(row['id'])
        cur.execute('INSERT INTO authors(name) VALUES (?)', (name,))
        return int(cur.lastrowid)


class ReviewHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, app):
        super().__init__(server_address, RequestHandlerClass)
        self.app = app


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = 'CalibreAIReview/0.1'

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in {'/', '/review'}:
            self._send_html(self.server.app.render_review_page())
            return
        if parsed.path == '/settings':
            self._send_html(self.server.app.render_settings_page())
            return
        if parsed.path == '/api/candidates':
            params = urllib.parse.parse_qs(parsed.query)
            limit = int(params.get('limit', [self.server.app.settings.data['max_rows']])[0])
            data = self.server.app.api_candidates(limit=limit)
            self._send_json(data)
            return
        if parsed.path == '/api/status':
            self._send_json(self.server.app.api_status())
            return
        if parsed.path == '/api/book':
            params = urllib.parse.parse_qs(parsed.query)
            if not params.get('id'):
                self._send_json({'error': 'missing id'}, status=400)
                return
            book = self.server.app.db.get_book(int(params['id'][0]))
            if not book:
                self._send_json({'error': 'not found'}, status=404)
                return
            suggestion = local_suggestion(book.title, book.authors, self.server.app.registry)
            self._send_json(self.server.app.book_to_json(book, suggestion))
            return
        self._send_json({'error': 'not found'}, status=404)

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == '/api/suggest':
            payload = self._read_json()
            book_id = int(payload['book_id'])
            use_ai = bool(payload.get('use_ai', False))
            book = self.server.app.db.get_book(book_id)
            if not book:
                self._send_json({'error': 'not found'}, status=404)
                return
            suggestion = self.server.app.suggest_book(book, use_ai=use_ai)
            self._send_json(self.server.app.book_to_json(book, suggestion))
            return
        if parsed.path == '/api/apply':
            payload = self._read_json()
            ids = [int(x) for x in payload.get('book_ids', [])]
            use_ai = bool(payload.get('use_ai', False))
            books = []
            for book_id in ids:
                book = self.server.app.db.get_book(book_id)
                if not book:
                    continue
                suggestion = self.server.app.suggest_book(book, use_ai=use_ai)
                if suggestion and suggestion.action == 'update':
                    book.suggestion = suggestion
                    books.append(book)
            changed = self.server.app.db.apply_rows(
                books,
                self.server.app.registry,
                self.server.app.backup_root,
                self.server.app.library_root,
            )
            self._send_json({'applied': changed, 'book_ids': ids})
            return
        if parsed.path == '/api/settings':
            payload = self._read_json()
            self.server.app.update_settings(payload)
            self._send_json({'ok': True})
            return
        if parsed.path == '/settings':
            payload = parse_settings_form(self._read_body())
            self.server.app.update_settings(payload)
            self.send_response(303)
            self.send_header('Location', '/settings')
            self.end_headers()
            return
        self._send_json({'error': 'not found'}, status=404)

    def log_message(self, fmt, *args):
        return

    def _read_json(self):
        return json.loads(self._read_body().decode('utf-8'))

    def _read_body(self):
        length = int(self.headers.get('Content-Length', '0'))
        return self.rfile.read(length) if length else b''

    def _send_json(self, payload: dict[str, Any], status: int = 200):
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html_text: str, status: int = 200):
        data = html_text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ReviewApp:
    def __init__(self, settings_path: Path):
        self.settings = SettingsStore(settings_path)
        self.registry = AuthorRegistry.load_default()
        self.db = CalibreReviewDB(Path(self.settings.data['db_path']))
        self.backup_root = Path(self.settings.data['backup_root'])
        self.library_root = Path(self.settings.data['library_root'])
        self.lock = threading.Lock()

    def update_settings(self, payload: dict[str, Any]):
        with self.lock:
            for key in ['db_path', 'library_root', 'backup_root', 'openai_base_url', 'openai_model']:
                if key in payload and payload[key]:
                    self.settings.data[key] = str(payload[key])
            for key in ['openai_api_key']:
                if key in payload and payload[key] is not None:
                    self.settings.data[key] = str(payload[key])
            for key in ['use_openai']:
                if key in payload:
                    self.settings.data[key] = bool(payload[key])
            if 'max_rows' in payload:
                self.settings.data['max_rows'] = max(1, int(payload['max_rows']))
            self.settings.save()
            self.registry = AuthorRegistry.load_default()
            self.db = CalibreReviewDB(Path(self.settings.data['db_path']))
            self.backup_root = Path(self.settings.data['backup_root'])
            self.library_root = Path(self.settings.data['library_root'])

    def suggest_book(self, book: BookRow, use_ai: bool = False) -> Suggestion:
        suggestion = local_suggestion(book.title, book.authors, self.registry)
        if suggestion is not None:
            return suggestion
        if use_ai and self.settings.data.get('use_openai'):
            api_key = resolve_api_key(self.settings.data.get('openai_api_key'))
            if api_key:
                prompt = ai_prompt_payload(book.book_id, book.title, book.authors, book.path, self.registry)
                try:
                    ai_suggestion = call_openai_responses(
                        api_key=api_key,
                        base_url=self.settings.data.get('openai_base_url', 'https://api.openai.com/v1'),
                        model=self.settings.data.get('openai_model', 'gpt-5.4-mini'),
                        prompt=prompt,
                        current_title=book.title,
                        current_authors=book.authors,
                    )
                    if ai_suggestion:
                        return ai_suggestion
                except Exception as err:
                    return Suggestion(
                        title=book.title,
                        authors=book.authors,
                        source='openai-error',
                        confidence=0.0,
                        reason=str(err),
                        action='leave',
                        raw={'error': str(err)},
                    )
        return Suggestion(
            title=book.title,
            authors=book.authors,
            source='none',
            confidence=0.0,
            reason='No obvious change.',
            action='leave',
            raw={},
        )

    def api_candidates(self, limit: int = 200) -> dict[str, Any]:
        rows = self.db.list_candidates(self.registry, limit=limit)
        suspicious = [self.book_to_json(book, book.suggestion or self.suggest_book(book)) for book in rows if book.suspicious or (book.suggestion and book.suggestion.action == 'update')]
        return {
            'count': len(suspicious),
            'rows': suspicious,
            'settings': {
                'db_path': str(self.settings.data['db_path']),
                'library_root': str(self.settings.data['library_root']),
                'openai_enabled': bool(self.settings.data.get('use_openai')),
            },
        }

    def api_status(self) -> dict[str, Any]:
        con = self.db.connect()
        try:
            books = con.execute('SELECT COUNT(*) AS c FROM books').fetchone()['c']
            authors = con.execute('SELECT COUNT(*) AS c FROM authors').fetchone()['c']
            return {
                'books': books,
                'authors': authors,
                'db_path': str(self.settings.data['db_path']),
                'library_root': str(self.settings.data['library_root']),
                'backup_root': str(self.settings.data['backup_root']),
            }
        finally:
            con.close()

    def book_to_json(self, book: BookRow, suggestion: Suggestion | None = None) -> dict[str, Any]:
        if suggestion is None:
            suggestion = self.suggest_book(book)
        return {
            'book_id': book.book_id,
            'title': book.title,
            'authors': book.authors,
            'path': book.path,
            'suggestion': {
                'title': suggestion.title,
                'authors': suggestion.authors,
                'source': suggestion.source,
                'confidence': suggestion.confidence,
                'reason': suggestion.reason,
                'action': suggestion.action,
            },
            'suspicious': bool(book.suspicious or (suggestion and suggestion.action == 'update')),
        }

    def render_review_page(self) -> str:
        settings = html.escape(json.dumps(self.settings.data, ensure_ascii=False, indent=2))
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Calibre AI Metadata Review</title>
  <style>
    :root {{
      --bg: #0b1020;
      --panel: #12192f;
      --panel-2: #18213b;
      --line: #28324f;
      --text: #eef2ff;
      --muted: #a9b2d6;
      --accent: #74d7ff;
      --accent-2: #8ce99a;
      --danger: #ff7b7b;
      --warn: #ffd166;
    }}
    body {{ margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background: linear-gradient(180deg, #070b16, #0b1020 30%, #111735); color: var(--text); }}
    header {{ padding: 24px; border-bottom: 1px solid var(--line); background: rgba(8,12,24,.85); position: sticky; top: 0; backdrop-filter: blur(12px); z-index: 3; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    .sub {{ color: var(--muted); font-size: 14px; }}
    .wrap {{ padding: 20px; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 16px; }}
    input, button, select, textarea {{ background: var(--panel-2); color: var(--text); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }}
    button {{ cursor: pointer; }}
    button.primary {{ background: linear-gradient(180deg, #2e8cff, #1864d8); border-color: #2e8cff; }}
    button.good {{ background: linear-gradient(180deg, #1f9d55, #0f7a3d); border-color: #1f9d55; }}
    button.warn {{ background: linear-gradient(180deg, #b08900, #8a6500); border-color: #b08900; }}
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 16px; }}
    .card {{ background: rgba(18,25,47,.92); border: 1px solid var(--line); border-radius: 16px; padding: 14px; }}
    .card .k {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    .card .v {{ font-size: 22px; margin-top: 6px; }}
    table {{ width: 100%; border-collapse: collapse; background: rgba(18,25,47,.8); border: 1px solid var(--line); border-radius: 16px; overflow: hidden; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px 12px; vertical-align: top; text-align: left; }}
    th {{ color: var(--muted); font-weight: 600; position: sticky; top: 94px; background: rgba(18,25,47,.97); z-index: 2; }}
    tr:hover td {{ background: rgba(255,255,255,.02); }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }}
    .badge {{ display: inline-block; padding: 3px 8px; border-radius: 999px; font-size: 12px; }}
    .b-update {{ background: rgba(116,215,255,.18); color: var(--accent); }}
    .b-leave {{ background: rgba(255,255,255,.08); color: var(--muted); }}
    .b-junk {{ background: rgba(255,123,123,.16); color: var(--danger); }}
    .b-local {{ background: rgba(140,233,154,.16); color: var(--accent-2); }}
    .reason {{ color: var(--muted); font-size: 13px; }}
    .side {{ margin-top: 16px; display: grid; grid-template-columns: 1fr; gap: 16px; }}
    details pre {{ white-space: pre-wrap; word-break: break-word; max-height: 260px; overflow: auto; }}
    a {{ color: var(--accent); }}
    .small {{ font-size: 12px; color: var(--muted); }}
    .row-actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .row-actions button {{ padding: 7px 10px; font-size: 12px; }}
    .right {{ margin-left: auto; }}
  </style>
</head>
<body>
  <header>
    <h1>Calibre AI Metadata Review</h1>
    <div class="sub">Browser admin for Calibre database cleanup. Registry + AI suggestions. Calibre-Web users can use this in the same browser flow.</div>
  </header>
  <div class="wrap">
    <div class="stats" id="stats"></div>
    <div class="toolbar">
      <input id="limit" type="number" min="25" max="1000" value="{int(self.settings.data['max_rows'])}" />
      <label><input type="checkbox" id="use-ai"> use OpenAI fallback</label>
      <button class="primary" id="refresh">Refresh queue</button>
      <button class="good" id="apply">Apply checked</button>
      <a href="/settings" class="right">Settings</a>
    </div>
    <table>
      <thead>
        <tr>
          <th style="width:40px"><input type="checkbox" id="check-all"></th>
          <th style="width:80px">ID</th>
          <th>Title</th>
          <th>Current authors</th>
          <th>Suggested authors</th>
          <th style="width:130px">Source</th>
          <th>Reason</th>
          <th style="width:170px">Actions</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
    <div class="side">
      <details open>
        <summary>Config snapshot</summary>
        <pre id="config-snapshot"></pre>
      </details>
      <details>
        <summary>Selected row details</summary>
        <pre id="details"></pre>
      </details>
    </div>
  </div>
  <script>
    const state = {{ rows: [], data: null }};
    const rowsEl = document.getElementById('rows');
    const statsEl = document.getElementById('stats');
    const detailsEl = document.getElementById('details');
    const configEl = document.getElementById('config-snapshot');
    const checkAll = document.getElementById('check-all');
    const useAiEl = document.getElementById('use-ai');
    const limitEl = document.getElementById('limit');

    useAiEl.checked = true;
    const settingsSnapshot = {json.dumps(self.settings.data, ensure_ascii=False, indent=2)};
    configEl.textContent = JSON.stringify(settingsSnapshot, null, 2);

    checkAll.addEventListener('change', () => {{
      document.querySelectorAll('input[data-row-check="1"]').forEach(cb => cb.checked = checkAll.checked);
    }});

    async function loadStatus() {{
      const resp = await fetch('/api/status');
      const data = await resp.json();
      statsEl.innerHTML = `
        <div class="card"><div class="k">Books</div><div class="v">${{data.books}}</div></div>
        <div class="card"><div class="k">Authors</div><div class="v">${{data.authors}}</div></div>
        <div class="card"><div class="k">DB</div><div class="v mono">${{escapeHtml(data.db_path)}}</div></div>
        <div class="card"><div class="k">Library</div><div class="v mono">${{escapeHtml(data.library_root)}}</div></div>
      `;
    }}

    function badgeFor(source, action) {{
      if (source === 'local-junk') return '<span class="badge b-junk">junk</span>';
      if (source === 'local-registry') return '<span class="badge b-local">registry</span>';
      if (source === 'openai') return '<span class="badge b-update">ai</span>';
      if (action === 'update') return '<span class="badge b-update">update</span>';
      return '<span class="badge b-leave">leave</span>';
    }}

    function escapeHtml(text) {{
      return String(text ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
    }}

    function renderRows(list) {{
      state.rows = list;
      rowsEl.innerHTML = list.map((row, idx) => {{
        const sug = row.suggestion || {{}};
        return `
          <tr data-index="${{idx}}">
            <td><input type="checkbox" data-row-check="1" ${{sug.action === 'update' ? 'checked' : ''}}></td>
            <td class="mono">${{row.book_id}}</td>
            <td><div>${{escapeHtml(row.title)}}</div><div class="small mono">${{escapeHtml(row.path)}}</div></td>
            <td>${{escapeHtml(row.authors.join(' | '))}}</td>
            <td>${{escapeHtml((sug.authors || row.authors).join(' | '))}}</td>
            <td>${{badgeFor(sug.source, sug.action)}}<div class="small mono">${{escapeHtml(sug.source || 'none')}}</div></td>
            <td class="reason">${{escapeHtml(sug.reason || row.reason || '')}}</td>
            <td>
              <div class="row-actions">
                <button class="warn" data-action="suggest">Suggest</button>
                <button data-action="inspect">Inspect</button>
              </div>
            </td>
          </tr>
        `;
      }}).join('');
      rowsEl.querySelectorAll('tr').forEach(tr => {{
        const idx = Number(tr.dataset.index);
        tr.addEventListener('click', async (ev) => {{
          const btn = ev.target.closest('button');
          if (!btn) {{
            showDetails(state.rows[idx]);
            return;
          }}
          const action = btn.dataset.action;
          if (action === 'suggest') {{
            const row = await suggestRow(state.rows[idx]);
            state.rows[idx] = row;
            renderRows(state.rows);
            showDetails(row);
          }}
          if (action === 'inspect') {{
            showDetails(state.rows[idx]);
          }}
        }});
      }});
    }}

    function showDetails(row) {{
      detailsEl.textContent = JSON.stringify(row, null, 2);
    }}

    async function loadQueue() {{
      const limit = Number(limitEl.value || 200);
      const resp = await fetch(`/api/candidates?limit=${{limit}}`);
      const data = await resp.json();
      renderRows(data.rows || []);
      showDetails((data.rows || [])[0] || {{}});
    }}

    async function suggestRow(row) {{
      const resp = await fetch('/api/suggest', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ book_id: row.book_id, use_ai: useAiEl.checked }})
      }});
      return await resp.json();
    }}

    async function applyChecked() {{
      const ids = [];
      document.querySelectorAll('tbody tr').forEach((tr, idx) => {{
        const cb = tr.querySelector('input[data-row-check="1"]');
        if (cb && cb.checked) ids.push(state.rows[idx].book_id);
      }});
      if (!ids.length) {{
        alert('No rows checked');
        return;
      }}
      const resp = await fetch('/api/apply', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ book_ids: ids, use_ai: useAiEl.checked }})
      }});
      const data = await resp.json();
      alert(`Applied to ${{data.applied}} book(s).`);
      await loadQueue();
      await loadStatus();
    }}

    document.getElementById('refresh').addEventListener('click', loadQueue);
    document.getElementById('apply').addEventListener('click', applyChecked);
    loadStatus();
    loadQueue();
  </script>
</body>
</html>"""

    def render_settings_page(self) -> str:
        data = self.settings.data
        def val(key):
            return html.escape(str(data.get(key, '')))
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Calibre AI Metadata Review - Settings</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #0b1020; color: #eef2ff; }}
    main {{ max-width: 820px; margin: 0 auto; padding: 24px; }}
    .card {{ background: #12192f; border: 1px solid #28324f; border-radius: 16px; padding: 20px; }}
    label {{ display: block; margin: 14px 0 6px; color: #a9b2d6; }}
    input {{ width: 100%; box-sizing: border-box; background: #18213b; color: #eef2ff; border: 1px solid #28324f; border-radius: 10px; padding: 10px 12px; }}
    .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    button, a.button {{ display: inline-block; margin-top: 18px; background: linear-gradient(180deg, #2e8cff, #1864d8); color: white; border: 0; border-radius: 10px; padding: 10px 14px; text-decoration: none; cursor: pointer; }}
    .muted {{ color: #a9b2d6; font-size: 14px; }}
    .top {{ display: flex; gap: 12px; align-items: center; justify-content: space-between; margin-bottom: 16px; }}
  </style>
</head>
<body>
<main>
  <div class="top">
    <h1>Settings</h1>
    <a class="button" href="/review">Back</a>
  </div>
  <div class="card">
    <form method="post" action="/settings">
      <label>Calibre DB path</label>
      <input name="db_path" value="{val('db_path')}" />
      <label>Library root</label>
      <input name="library_root" value="{val('library_root')}" />
      <label>Backup root</label>
      <input name="backup_root" value="{val('backup_root')}" />
      <div class="row">
        <div>
          <label>OpenAI API key</label>
          <input name="openai_api_key" value="{val('openai_api_key')}" />
        </div>
        <div>
          <label>OpenAI base URL</label>
          <input name="openai_base_url" value="{val('openai_base_url')}" />
        </div>
      </div>
      <div class="row">
        <div>
          <label>OpenAI model</label>
          <input name="openai_model" value="{val('openai_model')}" />
        </div>
        <div>
          <label>Max rows</label>
          <input name="max_rows" value="{val('max_rows')}" />
        </div>
      </div>
      <label><input type="checkbox" name="use_openai" {'checked' if data.get('use_openai') else ''} /> Use OpenAI fallback</label>
      <div class="muted">This settings file is stored at: {html.escape(str(self.settings.path))}</div>
      <button type="submit">Save</button>
    </form>
  </div>
</main>
</body>
</html>"""


def parse_settings_form(body: bytes) -> dict[str, Any]:
    parsed = urllib.parse.parse_qs(body.decode('utf-8'))
    out: dict[str, Any] = {}
    for key, values in parsed.items():
        if not values:
            continue
        value = values[0]
        if key == 'use_openai':
            out[key] = True
        elif key == 'max_rows':
            try:
                out[key] = int(value)
            except Exception:
                continue
        else:
            out[key] = value
    if 'use_openai' not in out:
        out['use_openai'] = False
    return out


def main():
    parser = argparse.ArgumentParser(description='Calibre AI metadata review web app.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8137)
    parser.add_argument('--settings', default=str(DEFAULT_SETTINGS_PATH))
    args = parser.parse_args()

    app = ReviewApp(Path(args.settings))

    class Handler(ReviewHandler):
        pass

    server = ReviewHTTPServer((args.host, args.port), Handler, app)
    print(f'Listening on http://{args.host}:{args.port}')
    server.serve_forever()


if __name__ == '__main__':
    main()
