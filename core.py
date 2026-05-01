from __future__ import annotations

import difflib
import json
from pathlib import Path
import pkgutil
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


JUNK_HINT_RE = re.compile(
    r'(\b(version|user|guide|book|thrillers?|unknown|unregistered|windows|analysis|story|stories|chapter|sponsor|service)\b|'
    r'(^\d{2}\.\d{4}$)|(^[A-Z]{2,}$)|(^[a-z]{2,}\d+$))',
    re.IGNORECASE,
)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize('NFKD', value)
    out: list[str] = []
    for ch in value:
        if unicodedata.combining(ch):
            continue
        cat = unicodedata.category(ch)
        if cat[0] in {'L', 'N'}:
            out.append(ch.lower())
        elif ch in {' ', '\t', '\n', '\r', '\f', '\v'}:
            out.append(' ')
        elif ch in {'’', '`', 'ʼ', '\'', '"', '|', '&', ';', '/', ',', '+', '.', '-', '(', ')'}:
            out.append(' ')
        else:
            out.append(' ')
    value = ''.join(out)
    value = re.sub(r'\s+', ' ', value).strip()
    return value


def pretty_authors(authors: list[str]) -> str:
    return ' | '.join(a for a in authors if a)


def split_author_blob(value: str) -> list[str]:
    if not value:
        return []
    if '|' in value or ';' in value:
        parts = re.split(r'\s*[|;]\s*', value)
        return [p.strip() for p in parts if p.strip()]
    return [value.strip()]


@dataclass
class Suggestion:
    title: str
    authors: list[str]
    source: str
    confidence: float
    reason: str
    action: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthorRegistry:
    canonical_author_map: dict[str, list[str]]
    junk_author_exact: set[str]
    valid_single_token_authors: set[str]
    alias_to_canonical: dict[str, str]
    normalized_alias_to_canonical: dict[str, str]
    canonical_normalized_to_canonical: dict[str, str]

    @classmethod
    def load_default(cls) -> 'AuthorRegistry':
        raw = cls._load_registry_bytes()
        if raw is None:
            raise FileNotFoundError('Bundled author registry not found')
        data = json.loads(raw.decode('utf-8'))
        canonical_author_map = data.get('canonical_author_map', {})
        junk = set(data.get('junk_author_exact', []))
        valid_single = set(data.get('valid_single_token_authors', []))

        alias_to_canonical: dict[str, str] = {}
        normalized_alias_to_canonical: dict[str, str] = {}
        canonical_normalized_to_canonical: dict[str, str] = {}
        for canonical, aliases in canonical_author_map.items():
            canonical_normalized_to_canonical[normalize_text(canonical)] = canonical
            for alias in aliases:
                if alias not in alias_to_canonical:
                    alias_to_canonical[alias] = canonical
                normalized_alias_to_canonical.setdefault(normalize_text(alias), canonical)
        return cls(
            canonical_author_map=canonical_author_map,
            junk_author_exact=junk,
            valid_single_token_authors=valid_single,
            alias_to_canonical=alias_to_canonical,
            normalized_alias_to_canonical=normalized_alias_to_canonical,
            canonical_normalized_to_canonical=canonical_normalized_to_canonical,
        )

    @staticmethod
    def _load_registry_bytes() -> bytes | None:
        if __package__:
            raw = pkgutil.get_data(__package__, 'data/author_registry_overrides.json')
            if raw is not None:
                return raw
        local_path = Path(__file__).resolve().parent / 'data' / 'author_registry_overrides.json'
        if local_path.exists():
            return local_path.read_bytes()
        return None

    def match_author(self, raw: str) -> str | None:
        if raw in self.alias_to_canonical:
            return self.alias_to_canonical[raw]
        normalized = normalize_text(raw)
        if normalized in self.normalized_alias_to_canonical:
            return self.normalized_alias_to_canonical[normalized]
        if normalized in self.canonical_normalized_to_canonical:
            return self.canonical_normalized_to_canonical[normalized]
        return None

    def is_junk(self, raw: str) -> bool:
        if raw in self.junk_author_exact:
            return True
        if normalize_text(raw) in {normalize_text(x) for x in self.junk_author_exact}:
            return True
        return bool(JUNK_HINT_RE.search(raw))

    def is_valid_single_token(self, raw: str) -> bool:
        return raw in self.valid_single_token_authors or normalize_text(raw) in {
            normalize_text(x) for x in self.valid_single_token_authors
        }

    def fuzzy_candidates(self, raw: str, limit: int = 5) -> list[str]:
        values = list(self.alias_to_canonical) + list(self.canonical_author_map)
        norm_map = {}
        for value in values:
            norm_map.setdefault(normalize_text(value), value)
        matches = difflib.get_close_matches(normalize_text(raw), list(norm_map), n=limit, cutoff=0.72)
        return [self.match_author(norm_map[m]) or norm_map[m] for m in matches]


def local_suggestion(title: str, authors: list[str], registry: AuthorRegistry) -> Suggestion | None:
    cleaned: list[str] = []
    changed = False
    for raw in authors or ['Unknown']:
        raw = raw.strip()
        if not raw:
            continue
        if registry.is_junk(raw):
            return Suggestion(
                title=title,
                authors=['Unknown'],
                source='local-junk',
                confidence=0.99,
                reason=f'Author string "{raw}" matches junk registry or junk hint.',
                action='update',
                raw={'raw_author': raw},
            )
        canonical = registry.match_author(raw)
        if canonical and canonical != raw:
            changed = True
            cleaned.append(canonical)
        else:
            cleaned.append(raw)

    if not cleaned:
        cleaned = ['Unknown']

    if changed:
        return Suggestion(
            title=title,
            authors=cleaned,
            source='local-registry',
            confidence=0.98,
            reason='Matched bundled author registry.',
            action='update',
            raw={},
        )

    if len(cleaned) == 1 and registry.is_valid_single_token(cleaned[0]):
        return Suggestion(
            title=title,
            authors=cleaned,
            source='local-valid',
            confidence=1.0,
            reason='Valid single-token author in registry whitelist.',
            action='leave',
            raw={},
        )

    return None


def ai_prompt_payload(book_id: int, title: str, authors: list[str], path: str, registry: AuthorRegistry) -> str:
    fuzzy = []
    for author in authors or ['Unknown']:
        cands = registry.fuzzy_candidates(author)
        if cands:
            fuzzy.append({'raw': author, 'candidates': cands[:3]})
    prompt = {
        'book_id': book_id,
        'title': title,
        'authors': authors,
        'path': path,
        'registry_candidates': fuzzy,
        'task': (
            'Decide whether the current Calibre metadata needs a correction. '
            'If the author is clearly wrong, return the canonical author list. '
            'If the title is also clearly wrong, return a better title. '
            'Preserve original-language author names when appropriate. '
            'If you are not confident, set action to leave.'
        ),
        'output_schema': {
            'action': 'leave|update',
            'title': 'string',
            'authors': ['string'],
            'confidence': '0..1',
            'reason': 'short string',
        },
    }
    return json.dumps(prompt, ensure_ascii=False, indent=2)


def parse_ai_response(text: str, current_title: str, current_authors: list[str]) -> Suggestion | None:
    if not text:
        return None
    payload = text.strip()
    if payload.startswith('```'):
        payload = re.sub(r'^```(?:json)?\s*', '', payload)
        payload = re.sub(r'\s*```$', '', payload)
    try:
        data = json.loads(payload)
    except Exception:
        m = re.search(r'\{.*\}', payload, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
    authors = data.get('authors')
    if isinstance(authors, str):
        authors = split_author_blob(authors)
    if not isinstance(authors, list) or not authors:
        authors = current_authors
    title = data.get('title') or current_title
    confidence = data.get('confidence', 0.0)
    try:
        confidence = float(confidence)
    except Exception:
        confidence = 0.0
    action = data.get('action', 'leave')
    reason = data.get('reason', '').strip() or 'AI suggestion'
    return Suggestion(
        title=title,
        authors=[str(a).strip() for a in authors if str(a).strip()],
        source='openai',
        confidence=max(0.0, min(1.0, confidence)),
        reason=reason,
        action=action if action in {'leave', 'update'} else 'leave',
        raw=data,
    )
