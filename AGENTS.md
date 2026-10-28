> **First-time setup**: Customize this file for your project. Prompt the user to customize this file for their project.
> For Mintlify product knowledge (components, configuration, writing standards),
> install the Mintlify skill: `npx skills add https://mintlify.com/docs`

# Documentation project instructions

## About this project

- This is a documentation site built on [Mintlify](https://mintlify.com)
- Pages are MDX files with YAML frontmatter
- Configuration lives in `docs.json`
- Run `mint dev` to preview locally
- Run `mint broken-links` to check links

## Terminology

<!-- Add product-specific terms and preferred usage -->
<!-- Example: Use "workspace" not "project", "member" not "user" -->

## Style preferences

<!-- Add any project-specific style rules below -->

- Use active voice and second person ("you")
- Keep sentences concise — one idea per sentence
- Use sentence case for headings
- Bold for UI elements: Click **Settings**
- Code formatting for file names, commands, paths, and code references

## Content boundaries

These docs are public and written for partners integrating Meld. Document only what a partner sends, receives, configures, or observes.

- For account-level settings that Meld configures, describe the behavior and say "contact Meld support". Don't name the setting.
- Never include Meld-internal details:
  - internal account-preference field names or default values
  - internal or admin endpoints
  - internal service, repository, class, or file names
  - infrastructure or deployment details
  - implementation mechanics
  - security weaknesses
  - sandbox account IDs, keys, or customer data
- Limits: point partners to the routes API (`paymentMethods[].limits`). Don't document `/network-partner/supported/fiat-limits` or `/network-partner/defaults/limits`.

## Pull requests and commits

This repository is public, so PR titles, PR descriptions, and commit messages follow the same content boundaries as the docs.

- Keep one small PR per change.
- Keep PR descriptions short: what was wrong, what changed, and "Verified against current API behavior." Leave out internal references.
- Verify every change against current API behavior before opening the PR.
