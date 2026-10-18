---
name: artefact-management
description: Manage offline artefacts per the objectory artefact-get-ready spec (download, install, checksum).
tools: ["read", "grep", "glob", "bash"]
---

# Artefact Management Skill

Manages the offline artefact objectory in the environment-specific repositories following the
objectory artefact-get-ready spec. Always operate from the objectory root.

List of environment and objectory root:
- Linux: `~/echo-home`
- Windows: `~/echo-home/windows`
- MacOS: `~/echo-home/workstation/macos`
- ChromeOS: `~/echo-home/workstation/chromium`

## Core layout (objectory root)

- `.properd/artefacts.yaml` — YSON inventory: `name: [ version , url , {suffix} ]`
- `.properd/artefact.sha256sum` — `sha256sum` of `./name-version.suffix`
- `artefacts/` — downloaded package storage; `artefacts/.function/geti/<name>` and
  `artefacts/.function/ready/<name>` hold per-name overrides.
- `.function/artefact-geti` — download entry point
- `.function/artefact-readi` — install/deploy entry point
- `.function/artefacts` — checksum verify / regenerate entry point

Spec references: `@Documents/specs/artefact-get-ready.md`,
`@Documents/specs/objectory.md`, `@Documents/specs/yson.md`.
Example function script: `@.function/artefacts`.

## Rules

1. **CWD first:** run every command from the objectory root. Scripts
   resolve paths relative to `$(pwd)`.
2. **Call the real scripts:** prefer `.function/artefact-geti`, `.function/artefact-readi`,
   `.function/artefacts` over manual curl/tar/sha256sum so overrides take effect.
3. **`all` convention:** omitting a name (or passing `all`) processes every inventory entry.
4. **Overrides win:** if `artefacts/.function/geti/$name` (resp. `ready/$name`) is readable,
   it is sourced and the default path returns early.
5. **Inventory format (YSON):** one entry per line,
   `name: [ version , url , {"suffix":"..."} ]`. Url/suffix may be absent when an override
   handles them (e.g. `python312`).
6. **Checksums:** `.properd/artefact.sha256sum` holds `hash  ./name-version.suffix` lines.
   `.function/artefacts` verifies all and auto-appends new names.
7. **No network without confirmation:** downloading (`geti`) is a network operation —
   inform the user and proceed only when confirmed.

## Get (download) — `.function/artefact-geti`

Usage:
```sh
.function/artefact-geti <name | all>
```

Behaviour:
- Reads `.properd/artefacts.yaml` for `[version, url, {suffix}]` of `<name>`.
- If `artefacts/.function/geti/<name>` exists and is readable, source it and return.
- Default: `curl -s -L -o artefacts/$name-$version.$suffix $url`.
- `all` loops over every inventory key.

Example inventory entry:
```yaml
opencode: [ 1.19.3 , "https://github.com/anomalyco/opencode/releases/download/v1.19.3/opencode-linux-x64.tar.gz", {"suffix":"tar.gz"}]
```
Override example: `artefacts/.function/geti/python312` rebuilds via `uv python install`.

## Ready (install/deploy) — `.function/artefact-readi`

Usage:
```sh
.function/artefact-readi <name | all>
```

Behaviour:
- Reads the same inventory fields.
- If `artefacts/.function/ready/<name>` exists and is readable, source it and return.
- Default: call the native installer/deployment system.
- `all` loops over every inventory key.

Override example: `artefacts/.function/ready/node` extracts a tarball into `$LOCAL_BIN/node`.

## Checksum — `.function/artefacts`

Usage:
```sh
.function/artefacts [<name>]
```

Behaviour:
- Ensures `.properd/artefact.sha256sum` exists (generates from `find` + `sha256sum` if absent).
- With `<name>`: checks `name-version` against the sum file.
- Without `<name>`: runs `sha256sum --check --ignore-missing` over the whole sum file.
- Auto-appends a hash entry for any `$name` found in `artefacts/` that is missing from the
  sum file.

## Editing the inventory

When adding a new artefact:
1. Append to `.properd/artefacts.yaml`:
   `newpkg: [ 1.2.3 , "https://example.com/pkg-1.2.3.tar.gz", {"suffix":"tar.gz"} ]`
2. Run `.function/artefact-geti newpkg` (confirm network first).
3. Run `.function/artefacts newpkg` to record the checksum.
4. Optionally add `artefacts/.function/geti/newpkg` or `artefacts/.function/ready/newpkg`
   for custom behaviour.
