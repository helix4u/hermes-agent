---
name: wiki-upkeep
description: Maintain a local Hermes/knowledge wiki — link checks, index pages, stale sections, and safe edits.
---

# Wiki upkeep

Use when the user wants to **clean up, validate, or reorganize** a wiki served by Hermes (`/wiki-host`) or a static HTML/Markdown wiki tree under their workspace.

## Principles

- **Read before write**: map structure with `search_files` / `read_file` before patching.
- **Small patches**: prefer `patch` over rewriting large files.
- **No secrets**: do not embed API keys or tokens in wiki pages.
- **Paths**: use the user’s wiki root (ask if unknown). Typical gateway entry is `knowledge_wiki.html` at the wiki host root.

## Checklist

1. **Index / navigation**: landing page links to major sections; fix broken relative URLs.
2. **Stale markers**: pages with `TODO`, `FIXME`, or dated “last updated” lines — confirm or refresh.
3. **Orphans**: pages not linked from the index (optional report to the user).
4. **Consistency**: titles, heading levels, and shared templates (sidebar, footer) match across pages.

## Suggested flow

1. Identify wiki root (env, user message, or `HERMES_HOME` / workspace convention).
2. `search_files` for `*.html`, `*.md` under that root (respect `.gitignore` / project layout).
3. Sample `read_file` on index and high-traffic pages.
4. Apply `patch` for fixes; use `write_file` only for new small pages.
5. Summarize **what changed** and **what to verify in the browser** (reload wiki URL).

## Optional automation

If the gateway hosts the wiki, remind the user that **`CHANGELOG.md`** (repo root) and user-facing behavior notes should stay in sync when wiki content documents product behavior.
