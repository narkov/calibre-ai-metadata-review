from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .core import parse_ai_response


def resolve_api_key(config_key: str | None = None) -> str:
    if config_key:
        return config_key.strip()
    return os.environ.get('OPENAI_API_KEY', '').strip()


def call_openai_responses(api_key: str, base_url: str, model: str, prompt: str, current_title: str, current_authors: list[str]):
    url = base_url.rstrip('/') + '/responses'
    body = {
        'model': model,
        'input': prompt,
        'temperature': 0,
        'max_output_tokens': 400,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    text = payload.get('output_text')
    if not text:
        text = _extract_text(payload)
    return parse_ai_response(text or '', current_title, current_authors)


def _extract_text(payload: dict) -> str:
    output = payload.get('output') or []
    pieces: list[str] = []
    for item in output:
        for content in item.get('content', []) or []:
            text = content.get('text')
            if text:
                pieces.append(text)
    return '\n'.join(pieces)

