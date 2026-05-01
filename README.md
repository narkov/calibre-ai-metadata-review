# Calibre AI Metadata Review

An interface-action plugin for Calibre that reviews selected books, applies canonical author normalization from a bundled registry, and can call OpenAI for ambiguous records.

## What it does

- Reviews the currently selected books in the Calibre GUI.
- Normalizes authors using the bundled registry.
- Flags junk-like author strings and title leaks.
- Optionally calls OpenAI Responses API for uncertain cases.
- Lets you preview and apply metadata fixes from inside Calibre.

## Install

1. Build the plugin ZIP with `calibre-customize -b .`
2. Install the ZIP in Calibre Preferences → Plugins or with `calibre-customize -a <zip>`

## Configuration

The plugin stores settings in Calibre preferences under `plugins/ai_metadata_review`.

Default OpenAI settings:

- API base URL: `https://api.openai.com/v1`
- Model: `gpt-5.4-mini`

The API key can be entered in the plugin settings or supplied via `OPENAI_API_KEY`.

## Notes

- The registry data bundled in `data/author_registry_overrides.json` is the same canonical map used in the existing metadata cleanup scripts.
- The plugin updates Calibre metadata only. It does not rename on-disk folders or rewrite source files.

