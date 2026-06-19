# Import Extraction for 13 Languages — Design Spec

**Issue:** #76
**Date:** 2026-06-18

## Background

`_extract_import_name()` in `mcp_server.py` currently handles Python, JavaScript/TypeScript, and Rust. All other languages in `_EXT_TO_LANG` have their extensions mapped but produce no `:depends-on` edges because (a) `_LANG_NODE_TYPES` is missing their entries and (b) `_extract_import_name` has no branch for them.

The 13 languages to add: Go, Java, C, C++, C#, Ruby, PHP, Kotlin, Swift, Scala, Haskell, Lua, Elixir.

## Section 1: Package Installation

Add 9 missing tree-sitter grammar packages to the fallback install list in `install.py`'s `check_tree_sitter_languages_package()`:

```
tree-sitter-c-sharp
tree-sitter-ruby
tree-sitter-php
tree-sitter-kotlin
tree-sitter-swift
tree-sitter-scala
tree-sitter-haskell
tree-sitter-lua
tree-sitter-elixir
```

These are installed at the same time as the existing individual packages (Go, Java, C, C++ etc.) in the Python 3.13+ fallback branch. No change to `pyproject.toml` — the project's dependency management for tree-sitter continues to be handled by `install.py`.

## Section 2: `_LANG_NODE_TYPES` Additions

Add entries for 12 new languages (Go already has an entry):

| Language key | `imports` node type(s) |
|---|---|
| `java` | `import_declaration` |
| `c` | `preproc_include` |
| `cpp` | `preproc_include` |
| `c_sharp` | `using_directive` |
| `ruby` | `call` |
| `php` | `include_expression`, `require_expression` |
| `kotlin` | `import_header` |
| `swift` | `import_declaration` |
| `scala` | `import_declaration` |
| `haskell` | `import` |
| `lua` | `function_call` |
| `elixir` | `call` |

Note: Ruby, Lua, and Elixir use general-purpose call/function-call nodes. `_walk_ast` passes all matching nodes to `_extract_import_name`, which filters on the callee name (`require`, `alias`, `import`, `use`).

## Section 3: `_extract_import_name` Branches and Helpers

### Approach: Option B — helpers for complex cases, inline for simple ones

Following the `_rust_use_root()` precedent, languages whose extraction requires more than a field lookup get dedicated top-level helper functions.

### Helper functions (new)

**`_c_include_name(node) -> Optional[str]`** — shared by C and C++
- Reads `system_lib_string` child (for `<stdio.h>`) or `string_literal` child (for `"myheader.h"`)
- Strips angle brackets or quotes, drops directory prefix, drops file extension
- Returns e.g. `"stdio"`, `"myheader"`

**`_csharp_using_name(node) -> Optional[str]`** — for C# `using_directive`
- Walks the qualified name child, extracts the first `.`-separated segment
- Returns e.g. `"System"` from `using System.Collections.Generic`

**`_ruby_require_name(node) -> Optional[str]`** — for Ruby `call` nodes
- Guards: method name must be `require` or `require_relative`
- Reads the string argument, strips quotes, returns `basename` without extension
- Returns e.g. `"rails"`, `"my_module"`

**`_lua_require_name(node) -> Optional[str]`** — for Lua `function_call` nodes
- Guards: function name must be `require`
- Reads string argument, returns it stripped of quotes
- Returns e.g. `"socket"`, `"mymodule"`

**`_elixir_module_name(node) -> Optional[str]`** — for Elixir `call` nodes
- Guards: call target must be `alias`, `import`, or `use`
- Reads first argument, returns first `.`-separated segment
- Returns e.g. `"MyApp"` from `alias MyApp.Router`

### Inline branches in `_extract_import_name`

**Go** — `import_declaration`:
- Walk named children; if child is `import_spec`, read its `path` field (strip quotes, take last `/`-segment)
- If child is `import_spec_list`, recurse into its `import_spec` children the same way
- Handles both `import "fmt"` and `import ("fmt"; "os")`

**Java** — `import_declaration`:
- Find `scoped_identifier` or `identifier` child, walk to the leftmost dotted segment
- Returns e.g. `"java"` from `import java.util.List`

**C / C++** — delegates to `_c_include_name(node)`

**C#** — delegates to `_csharp_using_name(node)`

**Ruby** — delegates to `_ruby_require_name(node)`

**PHP** — `include_expression` / `require_expression`:
- Read the string child, strip quotes, return basename without extension

**Kotlin** — `import_header`:
- Walk the identifier path (dotted), return first segment
- Returns e.g. `"kotlin"` from `import kotlin.collections.List`

**Swift** — `import_declaration`:
- Find `identifier` child directly, return its text
- Returns e.g. `"Foundation"`

**Scala** — `import_declaration`:
- Walk `stable_identifier` child, return first dotted segment
- Returns e.g. `"scala"` from `import scala.collection.mutable`

**Haskell** — `import`:
- Find `module` child, return first dotted segment
- Returns e.g. `"Data"` from `import Data.List`

**Lua** — delegates to `_lua_require_name(node)`

**Elixir** — delegates to `_elixir_module_name(node)`

## Section 4: Testing

Add `class TestExtractImportName` in `tests/test_mcp_server.py`.

One test per language:
1. Parse a minimal snippet using the real tree-sitter parser (no mocking)
2. Find the import node by its expected type
3. Call `_extract_import_name(node, lang_name)` directly
4. Assert the returned list contains the expected module name

Each test is guarded with `pytest.mark.skipif` checking that the grammar package is importable, so CI doesn't break on environments where optional grammars are absent.

The existing integration tests in `TestRunIngestionBitemporalDeps` remain unchanged — they continue to validate the full pipeline end-to-end for Python.
