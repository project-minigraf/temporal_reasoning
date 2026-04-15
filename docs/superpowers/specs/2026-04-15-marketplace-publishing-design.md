# Marketplace Publishing Design

**Date:** 2026-04-15
**Status:** Approved
**Scope:** GitHub-hosted plugin distribution; revert skill and repo name to `temporal-reasoning`

---

## Context

The ROADMAP identified two blockers for marketplace publishing:

1. `cargo install minigraf` requires a Rust toolchain — too high a barrier for general users.
2. The skill description leads with mechanism rather than user benefit.

minigraf v0.19.0 (2026-04-14) ships pre-built binaries for all target platforms, resolving blocker 1. This design covers the work needed to ship.

**Target marketplace:** GitHub-hosted plugin (self-published). Users add the repo to `extraKnownMarketplaces` in their Claude Code `settings.json`. The `claude-plugins-official` marketplace is a future consideration pending a relationship with its maintainers.

**Name revert:** The "Vulcan" rebrand carries trademark risk (existing software trademarks for "Vulcan" in tech). The skill and repo are reverting to `temporal-reasoning` / `temporal_reasoning`. The Python module (`vulcan.py`, `from vulcan import`) is out of scope for this change — it is internal API, not the public skill identity.

---

## Changes

### 1. GitHub repo rename

Rename `adityamukho/vulcan` → `adityamukho/temporal_reasoning` via GitHub repo settings (manual step — cannot be scripted). After renaming:

- Update `git remote set-url origin git@github.com:adityamukho/temporal_reasoning.git`
- GitHub automatically redirects the old URL, but all internal references should be updated.

### 2. Name revert across files

| File | Change |
|---|---|
| `SKILL.md` frontmatter | `name: vulcan` → `name: temporal-reasoning` |
| `SKILL.md` H1 / prose | Remove "Vulcan" brand header; use "Temporal Reasoning" as the project name throughout |
| `CLAUDE.md` | `# Vulcan — AI Coding Agent Memory` → `# Temporal Reasoning — AI Coding Agent Memory` |
| `AGENTS.md` | Same heading rename |
| `install.py` `SKILL_DIRS` | `skills/vulcan` → `skills/temporal-reasoning`; `.opencode/skills/vulcan` → `.opencode/skills/temporal-reasoning` |
| `README.md` | All "Vulcan" brand references → "Temporal Reasoning" |
| `ROADMAP.md` | Update repo URL and any "Vulcan" references |

The tool schemas (`tools/*.json`) use names like `vulcan_query`, `vulcan_transact`, etc. These are the Python-level tool identifiers and are **not** renamed in this change (same reason as `vulcan.py` — internal API, breaking change).

### 3. install.py — binary download

`check_minigraf()` is replaced by `ensure_minigraf()` with the following flow:

1. Check if `minigraf` is already on PATH and functional. If so, done.
2. Detect platform and architecture using `sys.platform` and `platform.machine()`.
3. Map to the appropriate GitHub release asset:

   | Platform | Asset |
   |---|---|
   | Linux x86_64 | `minigraf-x86_64-unknown-linux-gnu.tar.xz` |
   | Linux aarch64 | `minigraf-aarch64-unknown-linux-gnu.tar.xz` |
   | macOS arm64 | `minigraf-aarch64-apple-darwin.tar.xz` |
   | macOS x86_64 | `minigraf-x86_64-apple-darwin.tar.xz` |
   | Windows | `minigraf-x86_64-pc-windows-msvc.zip` |

4. Resolve the latest release version by following the GitHub releases/latest redirect.
5. Download the asset and its `.sha256` sidecar to a temp directory.
6. Verify SHA256 checksum before extracting.
7. Extract the binary to `~/.local/bin` (Linux/macOS) or `%LOCALAPPDATA%\Programs\minigraf` (Windows).
8. If no binary matches the detected platform, fall back to `cargo install minigraf` with a clear message explaining why.

**Dependencies:** stdlib only (`urllib.request`, `tarfile`, `zipfile`, `hashlib`, `platform`, `tempfile`). No new requirements.

**Install target directory selection:**
- Linux/macOS: `~/.local/bin` (created if absent; user is told to add it to PATH if not already present)
- Windows: `%LOCALAPPDATA%\Programs\minigraf\` (binary placed directly; user told to add to PATH if needed)

### 4. skill.json

Bump `requires.minigraf` from `>=0.18.0` to `>=0.19.0`.

### 5. SKILL.md — reframe description

The `## Dependencies` section replaces the `cargo install minigraf` instruction with: "Run `install.py` — it downloads the correct pre-built binary for your platform automatically."

The `## The Core Idea` opening paragraph is reordered to lead with the user-facing problem (context lost between sessions, repeated questions, contradicted decisions) before explaining the mechanism.

### 6. README.md — plugin install instructions

Two installation paths documented:

**Primary (plugin system):**
```json
"extraKnownMarketplaces": {
  "temporal-reasoning": {
    "source": {
      "source": "git",
      "url": "https://github.com/adityamukho/temporal_reasoning"
    }
  }
}
```
Then enable the plugin in Claude Code and run `install.py` once to download the minigraf binary.

**Fallback (manual):**
Clone the repo and run `install.py` directly. Suitable for environments without the plugin system or for development.

### 7. ROADMAP.md

Mark "Marketplace Publishing" as complete. Note that the pre-built binary blocker was resolved by minigraf v0.19.0.

---

## Out of Scope

- `claude-plugins-official` submission (no relationship with maintainers yet)
- Renaming `vulcan.py` or `vulcan_*` tool schema identifiers (internal API — breaking change deferred)
- Windows PATH mutation from within install.py (instruct the user instead)
- ARM Linux musl target (not shipped by minigraf v0.19.0; cargo fallback covers it)
- Automatic version pinning or update checks for the minigraf binary
