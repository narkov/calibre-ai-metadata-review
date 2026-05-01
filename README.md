# Calibre AI Metadata Review

A browser-based review tool for Calibre libraries that applies canonical author normalization from a bundled registry and can call OpenAI for ambiguous records.

## What it does

- Reviews the currently selected books in the Calibre GUI.
- Normalizes authors using the bundled registry.
- Flags junk-like author strings and title leaks.
- Optionally calls OpenAI Responses API for uncertain cases.
- Lets you preview and apply metadata fixes from inside Calibre.

## Web app

This repo now contains a standalone browser admin service in `web_app.py`.

Run it locally:

```bash
python3 web_app.py --host 127.0.0.1 --port 8137
```

Open:

```text
http://127.0.0.1:8137/review
```

The app reads Calibre metadata directly from `metadata.db` and writes back to the same database plus `metadata.opf` files.

## Configuration

The web app stores settings in a JSON file, defaulting to `/opt/calibre/config/ai_metadata_review_web.json`.

Default OpenAI settings:

- API base URL: `https://api.openai.com/v1`
- Model: `gpt-5.4-mini`

The API key can be entered in the settings page or supplied via `OPENAI_API_KEY`.

## Notes

- The registry data bundled in `data/author_registry_overrides.json` is the same canonical map used in the existing metadata cleanup scripts.
- The plugin updates Calibre metadata only. It does not rename on-disk folders or rewrite source files.
- The web app updates Calibre metadata and `metadata.opf` files in the library tree.
- If you still want the desktop Calibre plugin, the repo also keeps the plugin shell code, but the browser service is the primary interface for Calibre-Web users.
