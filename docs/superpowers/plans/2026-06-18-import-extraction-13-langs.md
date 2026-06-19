# Import Extraction for 13 Languages — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add language-specific import extraction for Go, Java, C, C++, C#, Ruby, PHP, Kotlin, Swift, Scala, Haskell, Lua, and Elixir so that `:depends-on` edges are written during git ingestion for all supported languages.

**Architecture:** Add 9 missing tree-sitter grammar packages to `install.py`; extend `_LANG_NODE_TYPES` in `mcp_server.py` with 12 new entries; add helper functions for complex languages (C/C++, C#, Ruby, Lua, Elixir) following the existing `_rust_use_root()` pattern; add inline branches for simple languages (Go, Java, Kotlin, Swift, Scala, Haskell, PHP) inside `_extract_import_name()`.

**Tech Stack:** Python 3.10+, tree-sitter 0.22+, individual `tree-sitter-<lang>` packages, pytest.

---

## File Map

| File | Changes |
|---|---|
| `install.py` | Add 9 packages to the individual-install fallback list (line 132–136) |
| `mcp_server.py` | Extend `_LANG_NODE_TYPES` (line 163); add 5 helper functions after `_rust_use_root`; add 13 `elif` branches in `_extract_import_name` |
| `tests/test_mcp_server.py` | Add `_find_node` helper and `TestExtractImportName` class at end of file |

---

## Task 1: Install 9 Missing Tree-Sitter Grammar Packages

**Files:**
- Modify: `install.py:131-136`

- [ ] **Step 1: Update install.py to include the 9 missing packages**

In `install.py`, replace the `individual` list (lines 131–136):

```python
    individual = [
        "tree-sitter>=0.22.0",
        "tree-sitter-rust", "tree-sitter-python", "tree-sitter-javascript",
        "tree-sitter-typescript", "tree-sitter-go", "tree-sitter-java",
        "tree-sitter-c", "tree-sitter-cpp",
        "tree-sitter-c-sharp", "tree-sitter-ruby", "tree-sitter-php",
        "tree-sitter-kotlin", "tree-sitter-swift", "tree-sitter-scala",
        "tree-sitter-haskell", "tree-sitter-lua", "tree-sitter-elixir",
    ]
```

- [ ] **Step 2: Install the 9 new packages into the venv**

```bash
.venv/bin/pip install \
  tree-sitter-c-sharp tree-sitter-ruby tree-sitter-php \
  tree-sitter-kotlin tree-sitter-swift tree-sitter-scala \
  tree-sitter-haskell tree-sitter-lua tree-sitter-elixir
```

Expected: each package installs successfully and lists `Successfully installed`.

- [ ] **Step 3: Verify all 9 packages are importable**

```bash
.venv/bin/python -c "
import tree_sitter_c_sharp, tree_sitter_ruby, tree_sitter_php
import tree_sitter_kotlin, tree_sitter_swift, tree_sitter_scala
import tree_sitter_haskell, tree_sitter_lua, tree_sitter_elixir
print('all ok')
"
```

Expected: `all ok`

- [ ] **Step 4: Commit**

```bash
git add install.py
git commit -m "feat(install): add 9 missing tree-sitter grammar packages for full language coverage"
```

---

## Task 2: Go Import Extraction

Go already has a `_LANG_NODE_TYPES` entry. This task only adds the extraction branch.

**Files:**
- Modify: `mcp_server.py:226-251` (`_extract_import_name`)
- Test: `tests/test_mcp_server.py` (append `TestExtractImportName` class)

- [ ] **Step 1: Add the test helper and Go test to test_mcp_server.py**

Append to the end of `tests/test_mcp_server.py`:

```python
# ---------------------------------------------------------------------------
# Helpers for TestExtractImportName
# ---------------------------------------------------------------------------

def _find_node(root, node_type: str):
    """DFS search for the first node matching node_type."""
    if root.type == node_type:
        return root
    for child in root.children:
        found = _find_node(child, node_type)
        if found:
            return found
    return None


def _parse_import_node(lang_name: str, source: bytes, node_type: str, tmp_path):
    """Parse source for lang_name, return first node of node_type or skip."""
    import mcp_server
    ext = {
        "go": ".go", "java": ".java", "c": ".c", "cpp": ".cpp",
        "c_sharp": ".cs", "ruby": ".rb", "php": ".php", "kotlin": ".kt",
        "swift": ".swift", "scala": ".scala", "haskell": ".hs",
        "lua": ".lua", "elixir": ".ex",
    }[lang_name]
    tmp_file = tmp_path / f"test{ext}"
    tmp_file.write_bytes(source)
    parser = mcp_server._get_parser(str(tmp_file))
    if parser is None:
        pytest.skip(f"No tree-sitter parser available for {lang_name}")
    tree = parser.parse(source)
    node = _find_node(tree.root_node, node_type)
    if node is None:
        pytest.fail(
            f"No {node_type!r} node found in AST.\n"
            f"Full AST sexp:\n{tree.root_node.sexp()}"
        )
    return node


class TestExtractImportName:
    """Unit tests for _extract_import_name — one per language, using real parsers."""

    def test_go_single_import(self, tmp_path):
        pytest.importorskip("tree_sitter_go")
        import mcp_server
        source = b'package main\nimport "fmt"'
        node = _parse_import_node("go", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "go")
        assert result == ["fmt"]

    def test_go_grouped_import(self, tmp_path):
        pytest.importorskip("tree_sitter_go")
        import mcp_server
        source = b'package main\nimport (\n\t"os"\n\t"github.com/user/pkg"\n)'
        node = _parse_import_node("go", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "go")
        assert "os" in result
        assert "pkg" in result
```

- [ ] **Step 2: Run the Go tests to confirm they fail**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_go_single_import tests/test_mcp_server.py::TestExtractImportName::test_go_grouped_import -v
```

Expected: FAIL — `_extract_import_name` returns `[]` for `lang_name == "go"`.

- [ ] **Step 3: Add the Go branch to `_extract_import_name`**

In `mcp_server.py`, inside `_extract_import_name`, after the `elif lang_name == "rust":` block (after line 250, before `return names`):

```python
    elif lang_name == "go":
        def _go_spec(spec_node):
            path = spec_node.child_by_field_name("path")
            if path:
                val = path.text.decode("utf-8").strip('"')
                names.append(val.split("/")[-1])

        for child in node.named_children:
            if child.type == "import_spec":
                _go_spec(child)
            elif child.type == "import_spec_list":
                for spec in child.named_children:
                    if spec.type == "import_spec":
                        _go_spec(spec)
```

- [ ] **Step 4: Run Go tests to confirm they pass**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_go_single_import tests/test_mcp_server.py::TestExtractImportName::test_go_grouped_import -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): add Go import extraction with tests"
```

---

## Task 3: Java Import Extraction

**Files:**
- Modify: `mcp_server.py:132-163` (`_LANG_NODE_TYPES`), `mcp_server.py:226-251` (`_extract_import_name`)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing Java test**

Append to `class TestExtractImportName` in `tests/test_mcp_server.py`:

```python
    def test_java_import(self, tmp_path):
        pytest.importorskip("tree_sitter_java")
        import mcp_server
        source = b'import java.util.List;'
        node = _parse_import_node("java", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "java")
        assert result == ["java"]
```

- [ ] **Step 2: Run to confirm it fails**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_java_import -v
```

Expected: FAIL — no `"java"` entry in `_LANG_NODE_TYPES`, so `_walk_ast` never reaches this node, and the branch is missing.

- [ ] **Step 3: Add Java to `_LANG_NODE_TYPES`**

In `mcp_server.py`, add after the `"go": {...}` block (line 162, before the closing `}`):

```python
    "java": {
        "functions": {"method_declaration"},
        "classes": {"class_declaration"},
        "imports": {"import_declaration"},
        "calls": {"method_invocation"},
    },
```

- [ ] **Step 4: Add the Java branch to `_extract_import_name`**

After the Go branch added in Task 2:

```python
    elif lang_name == "java":
        def _java_leftmost(n) -> Optional[str]:
            if n.type == "identifier":
                return n.text.decode("utf-8")
            for c in n.named_children:
                result = _java_leftmost(c)
                if result:
                    return result
            return None

        result = _java_leftmost(node)
        if result:
            names.append(result)
```

- [ ] **Step 5: Run Java test to confirm it passes**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_java_import -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): add Java import extraction with tests"
```

---

## Task 4: C and C++ Import Extraction

C and C++ share a helper `_c_include_name()` and the same node type `preproc_include`.

**Files:**
- Modify: `mcp_server.py:132-163` (`_LANG_NODE_TYPES`), `mcp_server.py:166` (new helpers), `mcp_server.py:226+` (`_extract_import_name`)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing C and C++ tests**

Append to `class TestExtractImportName`:

```python
    def test_c_system_include(self, tmp_path):
        pytest.importorskip("tree_sitter_c")
        import mcp_server
        source = b'#include <stdio.h>'
        node = _parse_import_node("c", source, "preproc_include", tmp_path)
        result = mcp_server._extract_import_name(node, "c")
        assert result == ["stdio"]

    def test_c_local_include(self, tmp_path):
        pytest.importorskip("tree_sitter_c")
        import mcp_server
        source = b'#include "myheader.h"'
        node = _parse_import_node("c", source, "preproc_include", tmp_path)
        result = mcp_server._extract_import_name(node, "c")
        assert result == ["myheader"]

    def test_cpp_include(self, tmp_path):
        pytest.importorskip("tree_sitter_cpp")
        import mcp_server
        source = b'#include <iostream>'
        node = _parse_import_node("cpp", source, "preproc_include", tmp_path)
        result = mcp_server._extract_import_name(node, "cpp")
        assert result == ["iostream"]
```

- [ ] **Step 2: Run to confirm they fail**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_c_system_include tests/test_mcp_server.py::TestExtractImportName::test_c_local_include tests/test_mcp_server.py::TestExtractImportName::test_cpp_include -v
```

Expected: FAIL

- [ ] **Step 3: Add C and C++ to `_LANG_NODE_TYPES`**

In `mcp_server.py`, add after the Java entry:

```python
    "c": {
        "functions": {"function_definition"},
        "classes": {"struct_specifier"},
        "imports": {"preproc_include"},
        "calls": {"call_expression"},
    },
    "cpp": {
        "functions": {"function_definition"},
        "classes": {"class_specifier", "struct_specifier"},
        "imports": {"preproc_include"},
        "calls": {"call_expression"},
    },
```

- [ ] **Step 4: Add `_c_include_name` helper**

In `mcp_server.py`, add the helper immediately after `_rust_use_root` (before `_extract_import_name`, around line 225):

```python
def _c_include_name(node) -> Optional[str]:
    """Return the header name (no path, no extension) from a C/C++ preproc_include node.

    Handles both:
      #include <stdio.h>   → system_lib_string → "stdio"
      #include "myheader.h" → string_literal    → "myheader"
    """
    import os
    for child in node.children:
        if child.type in ("system_lib_string", "string_literal"):
            raw = child.text.decode("utf-8").strip("<>\"'")
            return os.path.splitext(os.path.basename(raw))[0]
    return None
```

- [ ] **Step 5: Add C/C++ branches to `_extract_import_name`**

After the Java branch:

```python
    elif lang_name in ("c", "cpp"):
        name = _c_include_name(node)
        if name:
            names.append(name)
```

- [ ] **Step 6: Run C/C++ tests to confirm they pass**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_c_system_include tests/test_mcp_server.py::TestExtractImportName::test_c_local_include tests/test_mcp_server.py::TestExtractImportName::test_cpp_include -v
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): add C/C++ import extraction via _c_include_name helper"
```

---

## Task 5: C# Import Extraction

**Files:**
- Modify: `mcp_server.py` (`_LANG_NODE_TYPES`, new helper, `_extract_import_name`)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing C# test**

Append to `class TestExtractImportName`:

```python
    def test_csharp_using_simple(self, tmp_path):
        pytest.importorskip("tree_sitter_c_sharp")
        import mcp_server
        source = b'using System;'
        node = _parse_import_node("c_sharp", source, "using_directive", tmp_path)
        result = mcp_server._extract_import_name(node, "c_sharp")
        assert result == ["System"]

    def test_csharp_using_dotted(self, tmp_path):
        pytest.importorskip("tree_sitter_c_sharp")
        import mcp_server
        source = b'using System.Collections.Generic;'
        node = _parse_import_node("c_sharp", source, "using_directive", tmp_path)
        result = mcp_server._extract_import_name(node, "c_sharp")
        assert result == ["System"]
```

- [ ] **Step 2: Run to confirm they fail**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_csharp_using_simple tests/test_mcp_server.py::TestExtractImportName::test_csharp_using_dotted -v
```

Expected: FAIL

- [ ] **Step 3: Add C# to `_LANG_NODE_TYPES`**

```python
    "c_sharp": {
        "functions": {"method_declaration"},
        "classes": {"class_declaration"},
        "imports": {"using_directive"},
        "calls": {"invocation_expression"},
    },
```

- [ ] **Step 4: Add `_csharp_using_name` helper**

Add after `_c_include_name`:

```python
def _csharp_using_name(node) -> Optional[str]:
    """Return the root namespace from a C# using_directive node.

    using System;                    → "System"
    using System.Collections.Generic → "System"
    """
    def _first_ident(n) -> Optional[str]:
        if n.type == "identifier":
            return n.text.decode("utf-8")
        for c in n.named_children:
            result = _first_ident(c)
            if result:
                return result
        return None

    return _first_ident(node)
```

- [ ] **Step 5: Add C# branch to `_extract_import_name`**

```python
    elif lang_name == "c_sharp":
        name = _csharp_using_name(node)
        if name:
            names.append(name)
```

- [ ] **Step 6: Run C# tests to confirm they pass**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_csharp_using_simple tests/test_mcp_server.py::TestExtractImportName::test_csharp_using_dotted -v
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): add C# using-directive import extraction"
```

---

## Task 6: Ruby Import Extraction

Ruby uses `call` nodes for `require`/`require_relative`. `_LANG_NODE_TYPES` puts `call` in `imports` (not `calls`) so all `call` nodes pass through `_extract_import_name`; the helper filters on method name.

**Files:**
- Modify: `mcp_server.py` (`_LANG_NODE_TYPES`, new helper, `_extract_import_name`)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing Ruby tests**

Append to `class TestExtractImportName`:

```python
    def test_ruby_require(self, tmp_path):
        pytest.importorskip("tree_sitter_ruby")
        import mcp_server
        source = b"require 'rails'"
        node = _parse_import_node("ruby", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "ruby")
        assert result == ["rails"]

    def test_ruby_require_relative(self, tmp_path):
        pytest.importorskip("tree_sitter_ruby")
        import mcp_server
        source = b"require_relative 'my_module'"
        node = _parse_import_node("ruby", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "ruby")
        assert result == ["my_module"]

    def test_ruby_non_require_call_ignored(self, tmp_path):
        pytest.importorskip("tree_sitter_ruby")
        import mcp_server
        source = b"puts 'hello'"
        node = _parse_import_node("ruby", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "ruby")
        assert result == []
```

- [ ] **Step 2: Run to confirm they fail**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_ruby_require tests/test_mcp_server.py::TestExtractImportName::test_ruby_require_relative tests/test_mcp_server.py::TestExtractImportName::test_ruby_non_require_call_ignored -v
```

Expected: FAIL

- [ ] **Step 3: Add Ruby to `_LANG_NODE_TYPES`**

```python
    "ruby": {
        "functions": {"method"},
        "classes": {"class"},
        "imports": {"call"},
        "calls": set(),
    },
```

Note: `calls` is empty — Ruby regular calls are sacrificed to keep the `elif` logic clean. Ruby call-extraction is out of scope for this issue.

- [ ] **Step 4: Add `_ruby_require_name` helper**

Add after `_csharp_using_name`:

```python
def _ruby_require_name(node) -> Optional[str]:
    """Return the required module name from a Ruby call node.

    Handles:
      require 'rails'             → "rails"
      require_relative 'my_mod'  → "my_mod"
    Returns None for non-require calls.
    """
    import os
    method = node.child_by_field_name("method")
    if method is None:
        # Some grammars use the first identifier child instead of a named field
        for child in node.named_children:
            if child.type == "identifier":
                method = child
                break
    if method is None or method.text.decode("utf-8") not in ("require", "require_relative"):
        return None
    args = node.child_by_field_name("arguments")
    if args is None:
        for child in node.named_children:
            if child.type == "argument_list":
                args = child
                break
    if args is None:
        return None
    for child in args.named_children:
        if child.type in ("string", "simple_string", "string_literal"):
            # Try to get the string content node (strips the quote characters)
            content_node = next(
                (c for c in child.named_children if c.type in ("string_content", "string_value")),
                None,
            )
            if content_node:
                val = content_node.text.decode("utf-8")
            else:
                val = child.text.decode("utf-8").strip("'\"")
            return os.path.splitext(os.path.basename(val))[0]
    return None
```

- [ ] **Step 5: Add Ruby branch to `_extract_import_name`**

```python
    elif lang_name == "ruby":
        name = _ruby_require_name(node)
        if name:
            names.append(name)
```

- [ ] **Step 6: Run Ruby tests to confirm they pass**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_ruby_require tests/test_mcp_server.py::TestExtractImportName::test_ruby_require_relative tests/test_mcp_server.py::TestExtractImportName::test_ruby_non_require_call_ignored -v
```

Expected: PASS. If any test fails with unexpected node structure, inspect the AST:
```bash
.venv/bin/python -c "
import tree_sitter_ruby, tree_sitter
lang = tree_sitter.Language(tree_sitter_ruby.language())
p = tree_sitter.Parser(lang)
t = p.parse(b\"require 'rails'\")
print(t.root_node.sexp())
"
```
Then adjust field names in `_ruby_require_name` accordingly.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): add Ruby require/require_relative import extraction"
```

---

## Task 7: PHP Import Extraction

**Files:**
- Modify: `mcp_server.py` (`_LANG_NODE_TYPES`, `_extract_import_name`)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing PHP tests**

Append to `class TestExtractImportName`:

```python
    def test_php_require(self, tmp_path):
        pytest.importorskip("tree_sitter_php")
        import mcp_server
        source = b"<?php\nrequire 'config.php';"
        node = _parse_import_node("php", source, "require_expression", tmp_path)
        result = mcp_server._extract_import_name(node, "php")
        assert result == ["config"]

    def test_php_include(self, tmp_path):
        pytest.importorskip("tree_sitter_php")
        import mcp_server
        source = b"<?php\ninclude 'header.php';"
        node = _parse_import_node("php", source, "include_expression", tmp_path)
        result = mcp_server._extract_import_name(node, "php")
        assert result == ["header"]
```

- [ ] **Step 2: Run to confirm they fail**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_php_require tests/test_mcp_server.py::TestExtractImportName::test_php_include -v
```

Expected: FAIL. If the test fails with `No 'require_expression' node found`, inspect the AST:
```bash
.venv/bin/python -c "
import tree_sitter_php, tree_sitter
lang = tree_sitter.Language(tree_sitter_php.language_php())
p = tree_sitter.Parser(lang)
t = p.parse(b\"<?php\nrequire 'config.php';\")
print(t.root_node.sexp())
"
```
Note: `tree_sitter_php` exposes `language_php()` not `language()`. Adjust the `_get_parser` fallback if needed (see step 3 note).

- [ ] **Step 3: Add PHP to `_LANG_NODE_TYPES`**

```python
    "php": {
        "functions": {"function_definition", "method_declaration"},
        "classes": {"class_declaration"},
        "imports": {"require_expression", "include_expression",
                    "require_once_expression", "include_once_expression"},
        "calls": {"function_call_expression"},
    },
```

Note: PHP's tree-sitter package uses `language_php()` instead of `language()`. If `_get_parser` fails for `.php` files because `__import__("tree_sitter_php").language()` doesn't exist, add a special-case import in `_get_parser`:

```python
    # Attempt 2: individual tree-sitter-<lang> packages
    if parser is None:
        try:
            mod = __import__(f"tree_sitter_{lang_name}", fromlist=["language"])
            from tree_sitter import Language, Parser
            # PHP exposes language_php() instead of language()
            lang_fn = getattr(mod, f"language_{lang_name}", None) or mod.language
            lang_obj = Language(lang_fn())
            parser = Parser(lang_obj)
        except Exception:
            pass
```

- [ ] **Step 4: Add PHP branch to `_extract_import_name`**

```python
    elif lang_name == "php":
        import os
        for child in node.children:
            if child.type in ("string", "encapsed_string", "string_literal"):
                val = child.text.decode("utf-8").strip("'\"")
                names.append(os.path.splitext(os.path.basename(val))[0])
                break
```

- [ ] **Step 5: Run PHP tests to confirm they pass**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_php_require tests/test_mcp_server.py::TestExtractImportName::test_php_include -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): add PHP include/require import extraction"
```

---

## Task 8: Kotlin Import Extraction

**Files:**
- Modify: `mcp_server.py` (`_LANG_NODE_TYPES`, `_extract_import_name`)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing Kotlin test**

Append to `class TestExtractImportName`:

```python
    def test_kotlin_import(self, tmp_path):
        pytest.importorskip("tree_sitter_kotlin")
        import mcp_server
        source = b'import kotlin.collections.List'
        node = _parse_import_node("kotlin", source, "import_header", tmp_path)
        result = mcp_server._extract_import_name(node, "kotlin")
        assert result == ["kotlin"]
```

- [ ] **Step 2: Run to confirm it fails**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_kotlin_import -v
```

Expected: FAIL

- [ ] **Step 3: Add Kotlin to `_LANG_NODE_TYPES`**

```python
    "kotlin": {
        "functions": {"function_declaration"},
        "classes": {"class_declaration"},
        "imports": {"import_header"},
        "calls": {"call_expression"},
    },
```

- [ ] **Step 4: Add Kotlin branch to `_extract_import_name`**

```python
    elif lang_name == "kotlin":
        # import_header contains a dotted identifier path
        # Walk to leftmost simple_identifier or identifier child
        def _kotlin_first_seg(n) -> Optional[str]:
            if n.type in ("simple_identifier", "identifier"):
                return n.text.decode("utf-8")
            for c in n.named_children:
                result = _kotlin_first_seg(c)
                if result:
                    return result
            return None

        result = _kotlin_first_seg(node)
        if result:
            names.append(result)
```

- [ ] **Step 5: Run Kotlin test to confirm it passes**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_kotlin_import -v
```

Expected: PASS. If it fails with wrong result, inspect:
```bash
.venv/bin/python -c "
import tree_sitter_kotlin, tree_sitter
lang = tree_sitter.Language(tree_sitter_kotlin.language())
p = tree_sitter.Parser(lang)
t = p.parse(b'import kotlin.collections.List')
print(t.root_node.sexp())
"
```

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): add Kotlin import_header extraction"
```

---

## Task 9: Swift Import Extraction

**Files:**
- Modify: `mcp_server.py` (`_LANG_NODE_TYPES`, `_extract_import_name`)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing Swift test**

Append to `class TestExtractImportName`:

```python
    def test_swift_import(self, tmp_path):
        pytest.importorskip("tree_sitter_swift")
        import mcp_server
        source = b'import Foundation'
        node = _parse_import_node("swift", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "swift")
        assert result == ["Foundation"]
```

- [ ] **Step 2: Run to confirm it fails**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_swift_import -v
```

Expected: FAIL

- [ ] **Step 3: Add Swift to `_LANG_NODE_TYPES`**

```python
    "swift": {
        "functions": {"function_declaration"},
        "classes": {"class_declaration"},
        "imports": {"import_declaration"},
        "calls": {"call_expression"},
    },
```

- [ ] **Step 4: Add Swift branch to `_extract_import_name`**

```python
    elif lang_name == "swift":
        # import_declaration → identifier child is the module name
        for child in node.named_children:
            if child.type in ("identifier", "simple_identifier"):
                names.append(child.text.decode("utf-8"))
                break
```

- [ ] **Step 5: Run Swift test to confirm it passes**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_swift_import -v
```

Expected: PASS. If it fails, inspect:
```bash
.venv/bin/python -c "
import tree_sitter_swift, tree_sitter
lang = tree_sitter.Language(tree_sitter_swift.language())
p = tree_sitter.Parser(lang)
t = p.parse(b'import Foundation')
print(t.root_node.sexp())
"
```

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): add Swift import_declaration extraction"
```

---

## Task 10: Scala Import Extraction

**Files:**
- Modify: `mcp_server.py` (`_LANG_NODE_TYPES`, `_extract_import_name`)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing Scala test**

Append to `class TestExtractImportName`:

```python
    def test_scala_import(self, tmp_path):
        pytest.importorskip("tree_sitter_scala")
        import mcp_server
        source = b'import scala.collection.mutable'
        node = _parse_import_node("scala", source, "import_declaration", tmp_path)
        result = mcp_server._extract_import_name(node, "scala")
        assert result == ["scala"]
```

- [ ] **Step 2: Run to confirm it fails**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_scala_import -v
```

Expected: FAIL

- [ ] **Step 3: Add Scala to `_LANG_NODE_TYPES`**

```python
    "scala": {
        "functions": {"function_definition"},
        "classes": {"class_definition"},
        "imports": {"import_declaration"},
        "calls": {"call_expression"},
    },
```

- [ ] **Step 4: Add Scala branch to `_extract_import_name`**

```python
    elif lang_name == "scala":
        # import_declaration contains a path; take the first dotted segment
        for child in node.named_children:
            txt = child.text.decode("utf-8")
            names.append(txt.split(".")[0])
            break
```

- [ ] **Step 5: Run Scala test to confirm it passes**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_scala_import -v
```

Expected: PASS. If it fails, inspect:
```bash
.venv/bin/python -c "
import tree_sitter_scala, tree_sitter
lang = tree_sitter.Language(tree_sitter_scala.language())
p = tree_sitter.Parser(lang)
t = p.parse(b'import scala.collection.mutable')
print(t.root_node.sexp())
"
```

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): add Scala import_declaration extraction"
```

---

## Task 11: Haskell Import Extraction

**Files:**
- Modify: `mcp_server.py` (`_LANG_NODE_TYPES`, `_extract_import_name`)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing Haskell test**

Append to `class TestExtractImportName`:

```python
    def test_haskell_import(self, tmp_path):
        pytest.importorskip("tree_sitter_haskell")
        import mcp_server
        source = b'import Data.List'
        node = _parse_import_node("haskell", source, "import", tmp_path)
        result = mcp_server._extract_import_name(node, "haskell")
        assert result == ["Data"]
```

- [ ] **Step 2: Run to confirm it fails**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_haskell_import -v
```

Expected: FAIL

- [ ] **Step 3: Add Haskell to `_LANG_NODE_TYPES`**

```python
    "haskell": {
        "functions": {"function"},
        "classes": {"data_type"},
        "imports": {"import"},
        "calls": {"apply"},
    },
```

- [ ] **Step 4: Add Haskell branch to `_extract_import_name`**

```python
    elif lang_name == "haskell":
        # import node contains the module name; find a module or constructor child
        for child in node.named_children:
            if child.type in ("module", "qualified_module", "constructor"):
                txt = child.text.decode("utf-8")
                names.append(txt.split(".")[0])
                break
```

- [ ] **Step 5: Run Haskell test to confirm it passes**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_haskell_import -v
```

Expected: PASS. If it fails, inspect:
```bash
.venv/bin/python -c "
import tree_sitter_haskell, tree_sitter
lang = tree_sitter.Language(tree_sitter_haskell.language())
p = tree_sitter.Parser(lang)
t = p.parse(b'import Data.List')
print(t.root_node.sexp())
"
```

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): add Haskell import extraction"
```

---

## Task 12: Lua Import Extraction

Lua uses `function_call` nodes for `require(...)`. Like Ruby, `function_call` goes in `imports` to filter via helper.

**Files:**
- Modify: `mcp_server.py` (`_LANG_NODE_TYPES`, new helper, `_extract_import_name`)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing Lua tests**

Append to `class TestExtractImportName`:

```python
    def test_lua_require(self, tmp_path):
        pytest.importorskip("tree_sitter_lua")
        import mcp_server
        source = b'require("socket")'
        node = _parse_import_node("lua", source, "function_call", tmp_path)
        result = mcp_server._extract_import_name(node, "lua")
        assert result == ["socket"]

    def test_lua_non_require_ignored(self, tmp_path):
        pytest.importorskip("tree_sitter_lua")
        import mcp_server
        source = b'print("hello")'
        node = _parse_import_node("lua", source, "function_call", tmp_path)
        result = mcp_server._extract_import_name(node, "lua")
        assert result == []
```

- [ ] **Step 2: Run to confirm they fail**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_lua_require tests/test_mcp_server.py::TestExtractImportName::test_lua_non_require_ignored -v
```

Expected: FAIL

- [ ] **Step 3: Add Lua to `_LANG_NODE_TYPES`**

```python
    "lua": {
        "functions": {"function_definition"},
        "classes": set(),
        "imports": {"function_call"},
        "calls": set(),
    },
```

- [ ] **Step 4: Add `_lua_require_name` helper**

Add after `_ruby_require_name`:

```python
def _lua_require_name(node) -> Optional[str]:
    """Return the module name from a Lua function_call to require().

    require("socket")  → "socket"
    require "lfs"      → "lfs"
    Returns None for non-require calls.
    """
    # The function name is usually the first child (identifier or prefix_expression)
    fn_node = None
    for child in node.children:
        if child.type in ("identifier", "name"):
            fn_node = child
            break
    if fn_node is None or fn_node.text.decode("utf-8") != "require":
        return None
    # Arguments: args node contains the string
    for child in node.named_children:
        if child.type in ("args", "argument_list"):
            for arg in child.named_children:
                if arg.type == "string":
                    return arg.text.decode("utf-8").strip("'\"()")
            # fallback: the arg itself is a string
            if child.type == "string":
                return child.text.decode("utf-8").strip("'\"")
    return None
```

- [ ] **Step 5: Add Lua branch to `_extract_import_name`**

```python
    elif lang_name == "lua":
        name = _lua_require_name(node)
        if name:
            names.append(name)
```

- [ ] **Step 6: Run Lua tests to confirm they pass**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_lua_require tests/test_mcp_server.py::TestExtractImportName::test_lua_non_require_ignored -v
```

Expected: PASS. If it fails, inspect:
```bash
.venv/bin/python -c "
import tree_sitter_lua, tree_sitter
lang = tree_sitter.Language(tree_sitter_lua.language())
p = tree_sitter.Parser(lang)
t = p.parse(b'require(\"socket\")')
print(t.root_node.sexp())
"
```

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): add Lua require() import extraction"
```

---

## Task 13: Elixir Import Extraction

Elixir uses `call` nodes for `alias`, `import`, and `use`.

**Files:**
- Modify: `mcp_server.py` (`_LANG_NODE_TYPES`, new helper, `_extract_import_name`)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing Elixir tests**

Append to `class TestExtractImportName`:

```python
    def test_elixir_alias(self, tmp_path):
        pytest.importorskip("tree_sitter_elixir")
        import mcp_server
        source = b'alias MyApp.Router'
        node = _parse_import_node("elixir", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "elixir")
        assert result == ["MyApp"]

    def test_elixir_import(self, tmp_path):
        pytest.importorskip("tree_sitter_elixir")
        import mcp_server
        source = b'import Ecto.Query'
        node = _parse_import_node("elixir", source, "call", tmp_path)
        result = mcp_server._extract_import_name(node, "elixir")
        assert result == ["Ecto"]

    def test_elixir_non_module_call_ignored(self, tmp_path):
        pytest.importorskip("tree_sitter_elixir")
        import mcp_server
        source = b'IO.puts("hello")'
        # IO.puts is a dot_call or qualified_call, not a plain call — skip if no call node
        try:
            node = _parse_import_node("elixir", source, "call", tmp_path)
        except pytest.fail.Exception:
            return  # no call node at all — that's fine
        result = mcp_server._extract_import_name(node, "elixir")
        assert result == []
```

- [ ] **Step 2: Run to confirm they fail**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_elixir_alias tests/test_mcp_server.py::TestExtractImportName::test_elixir_import tests/test_mcp_server.py::TestExtractImportName::test_elixir_non_module_call_ignored -v
```

Expected: FAIL

- [ ] **Step 3: Add Elixir to `_LANG_NODE_TYPES`**

```python
    "elixir": {
        "functions": {"def", "defp"},
        "classes": {"defmodule"},
        "imports": {"call"},
        "calls": set(),
    },
```

- [ ] **Step 4: Add `_elixir_module_name` helper**

Add after `_lua_require_name`:

```python
def _elixir_module_name(node) -> Optional[str]:
    """Return the root module name from an Elixir alias/import/use call node.

    alias MyApp.Router     → "MyApp"
    import Ecto.Query      → "Ecto"
    use Phoenix.Controller → "Phoenix"
    Returns None for non-module calls.
    """
    _ELIXIR_MODULE_CALLS = {"alias", "import", "use", "require"}
    # The call target (function name) is usually the first named child
    target = node.child_by_field_name("target")
    if target is None:
        for child in node.named_children:
            if child.type in ("identifier", "atom"):
                target = child
                break
    if target is None or target.text.decode("utf-8") not in _ELIXIR_MODULE_CALLS:
        return None
    # Arguments contain the module name
    args = node.child_by_field_name("arguments")
    if args is None:
        for child in node.named_children:
            if child.type in ("arguments", "argument_list"):
                args = child
                break
    if args is None:
        return None
    for child in args.named_children:
        txt = child.text.decode("utf-8")
        return txt.split(".")[0]
    return None
```

- [ ] **Step 5: Add Elixir branch to `_extract_import_name`**

```python
    elif lang_name == "elixir":
        name = _elixir_module_name(node)
        if name:
            names.append(name)
```

- [ ] **Step 6: Run Elixir tests to confirm they pass**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName::test_elixir_alias tests/test_mcp_server.py::TestExtractImportName::test_elixir_import tests/test_mcp_server.py::TestExtractImportName::test_elixir_non_module_call_ignored -v
```

Expected: PASS. If it fails, inspect:
```bash
.venv/bin/python -c "
import tree_sitter_elixir, tree_sitter
lang = tree_sitter.Language(tree_sitter_elixir.language())
p = tree_sitter.Parser(lang)
t = p.parse(b'alias MyApp.Router')
print(t.root_node.sexp())
"
```

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "feat(ingest): add Elixir alias/import/use extraction"
```

---

## Task 14: Full Test Run and Final Commit

- [ ] **Step 1: Run the full test suite**

```bash
.venv/bin/pytest tests/test_mcp_server.py -v 2>&1 | tail -30
```

Expected: all `TestExtractImportName` tests pass; no regressions in existing tests.

- [ ] **Step 2: Run only the new extraction tests as a final check**

```bash
.venv/bin/pytest tests/test_mcp_server.py::TestExtractImportName -v
```

Expected: all 20+ tests pass (or skip with `SKIP` reason if parser unavailable in CI).

- [ ] **Step 3: Close the issue**

```bash
gh issue close 76 --comment "All 13 languages implemented with tests. See commits in this branch."
```
