"""Repository indexer — walk, chunk, embed, store pipeline backed by PostgreSQL."""

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from postgres_client import get_connection, release_connection
from .chunker import ContentChunker
from .db import ContextDB
from .language import MemoryBankDetector
from .walker import FileWalker

logger = logging.getLogger(__name__)


class _CancelledError(Exception):
    """Raised when indexing is cancelled by user."""
    pass

# Global indexing state for progress tracking
_indexing_lock = threading.Lock()
_indexing_status: Dict[str, Any] = {}  # {project: {status, progress, total, ...}}
_cancel_flags: Dict[str, bool] = {}   # {project: True} to request cancellation


def get_indexing_status() -> Dict[str, Any]:
    with _indexing_lock:
        return dict(_indexing_status)


def request_cancel(project: str):
    """Request cancellation of an in-progress indexing job."""
    with _indexing_lock:
        _cancel_flags[project] = True
        if project in _indexing_status:
            _indexing_status[project]["phase"] = "Cancelling"


def _is_cancelled(project: str) -> bool:
    with _indexing_lock:
        return _cancel_flags.get(project, False)


def _clear_cancel(project: str):
    with _indexing_lock:
        _cancel_flags.pop(project, None)


def _set_status(project: str, **kwargs):
    kwargs.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
    with _indexing_lock:
        if project not in _indexing_status:
            _indexing_status[project] = {}
        _indexing_status[project].update(kwargs)


def _clear_status(project: str):
    with _indexing_lock:
        _indexing_status.pop(project, None)


def _schedule_status_cleanup(project: str):
    def cleanup():
        import time
        time.sleep(30)
        _clear_status(project)
    threading.Thread(target=cleanup, daemon=True).start()


class Indexer:
    """Repository indexer with background threading and progress tracking."""

    def __init__(self):
        self.chunker = ContentChunker()
        self._embedder = None  # lazy loaded

    def _get_embedder(self):
        if self._embedder is None:
            from .embeddings import EmbeddingModel
            self._embedder = EmbeddingModel.get()
        return self._embedder

    def _detect_language(self, rel_path: str):
        """Detect language using MemoryBankDetector + Pygments fallback."""
        language, is_memory_bank = MemoryBankDetector.detect_language(rel_path)
        if is_memory_bank:
            return language, True

        try:
            from pygments.lexers import get_lexer_for_filename
            from pygments.util import ClassNotFound
            try:
                lexer = get_lexer_for_filename(rel_path)
                return lexer.name, False
            except ClassNotFound:
                pass
        except ImportError:
            pass

        return language, False

    EXTENSION_TO_LANG = {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".mts": "typescript",
        ".cts": "typescript",
        ".vue": "html",
        ".svelte": "html",
        ".astro": "html",
        ".html": "html",
        ".htm": "html",
        ".css": "css",
        ".scss": "css",
        ".less": "css",
        ".rb": "ruby",
        ".java": "java",
        ".cpp": "cpp",
        ".hpp": "cpp",
        ".cc": "cpp",
        ".cxx": "cpp",
        ".c": "c",
        ".h": "c",
        ".go": "go",
        ".rs": "rust",
        ".php": "php",
        ".cs": "c_sharp",
        ".swift": "swift",
        ".scala": "scala",
        ".kt": "kotlin",
        ".kts": "kotlin",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "bash",
        ".fish": "bash",
        ".dart": "dart",
        ".ex": "elixir",
        ".exs": "elixir",
        ".erl": "erlang",
        ".hrl": "erlang",
        ".lua": "lua",
        ".r": "r",
        ".sql": "sql",
        ".m": "objc",
        ".mm": "objc",
        ".sol": "solidity",
        ".graphql": "graphql",
        ".gql": "graphql",
        ".hcl": "hcl",
        ".tf": "hcl",
        ".tfvars": "hcl",
        ".json": "json",
        ".jsonc": "json",
        ".toml": "toml",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".md": "markdown",
        ".mdx": "markdown",
    }

    AST_QUERIES = {
        "python": """
            (class_definition name: (identifier) @class.name) @class
            (function_definition name: (identifier) @function.name) @function
        """,
        "javascript": """
            (class_declaration name: (identifier) @class.name) @class
            (function_declaration name: (identifier) @function.name) @function
            (method_definition name: (property_identifier) @function.name) @function
        """,
        "typescript": """
            (class_declaration name: (type_identifier) @class.name) @class
            (interface_declaration name: (type_identifier) @class.name) @class
            (function_declaration name: (identifier) @function.name) @function
            (method_definition name: (property_identifier) @function.name) @function
        """,
        "tsx": """
            (class_declaration name: (type_identifier) @class.name) @class
            (interface_declaration name: (type_identifier) @class.name) @class
            (function_declaration name: (identifier) @function.name) @function
            (method_definition name: (property_identifier) @function.name) @function
        """,
        "ruby": """
            (class name: [ (constant) (scope_resolution) ] @class.name) @class
            (module name: [ (constant) (scope_resolution) ] @class.name) @class
            (method name: (identifier) @function.name) @function
        """,
        "go": """
            (type_declaration (type_spec name: (type_identifier) @class.name)) @class
            (function_declaration name: (identifier) @function.name) @function
            (method_declaration name: (field_identifier) @function.name) @function
        """,
        "java": """
            (class_declaration name: (identifier) @class.name) @class
            (interface_declaration name: (identifier) @class.name) @class
            (method_declaration name: (identifier) @function.name) @function
        """,
        "cpp": """
            (class_specifier name: [ (type_identifier) (template_type) ] @class.name) @class
            (struct_specifier name: [ (type_identifier) (template_type) ] @class.name) @class
            (function_definition declarator: [
                (function_declarator declarator: (identifier) @function.name)
                (pointer_declarator declarator: (function_declarator declarator: (identifier) @function.name))
                (reference_declarator declarator: (function_declarator declarator: (identifier) @function.name))
            ]) @function
        """,
        "rust": """
            (struct_item name: (type_identifier) @class.name) @class
            (enum_item name: (type_identifier) @class.name) @class
            (trait_item name: (type_identifier) @class.name) @class
            (impl_item type: (type_identifier) @class.name) @class
            (function_item name: (identifier) @function.name) @function
        """,
        "php": """
            (class_declaration name: (name) @class.name) @class
            (interface_declaration name: (name) @class.name) @class
            (method_declaration name: (name) @function.name) @function
            (function_definition name: (name) @function.name) @function
        """,
        "scala": """
            (class_definition name: (identifier) @class.name) @class
            (object_definition name: (identifier) @class.name) @class
            (trait_definition name: (identifier) @class.name) @class
            (function_definition name: (identifier) @function.name) @function
        """,
    }

    _REGEX_AST_PATTERNS = {
        "scala": [
            (r"^\s*(?:abstract\s+)?(?:case\s+)?class\s+(\w+)", "class"),
            (r"^\s*(?:case\s+)?object\s+(\w+)", "class"),
            (r"^\s*trait\s+(\w+)", "class"),
            (r"^\s*(?:override\s+)?(?:final\s+)?(?:private\s+)?(?:protected\s+)?def\s+(\w+)", "function"),
        ],
        "javascript": [
            (r"^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)", "class"),
            (r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function"),
            (r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(", "function"),
            (r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function", "function"),
            (r"^\s+(?:static\s+)?(?:async\s+)?(?:get\s+|set\s+)?(\w+)\s*\([^)]*\)\s*\{", "function"),
        ],
        "ruby": [
            (r"^\s*class\s+(\w+)", "class"),
            (r"^\s*module\s+(\w+)", "class"),
            (r"^\s*def\s+(\w+)", "function"),
        ],
        "java": [
            (r"^\s*(?:public\s+|private\s+|protected\s+)?(?:abstract\s+|final\s+)?class\s+(\w+)", "class"),
            (r"^\s*(?:public\s+|private\s+|protected\s+)?interface\s+(\w+)", "class"),
            (r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|final\s+)*\w[\w<>,\s]*\s+(\w+)\s*\(", "function"),
        ],
        "go": [
            (r"^\s*type\s+(\w+)\s+struct", "class"),
            (r"^\s*type\s+(\w+)\s+interface", "class"),
            (r"^\s*func\s+(?:\([^)]*\)\s+)?(\w+)\s*\(", "function"),
        ],
        "rust": [
            (r"^\s*(?:pub\s+)?struct\s+(\w+)", "class"),
            (r"^\s*(?:pub\s+)?enum\s+(\w+)", "class"),
            (r"^\s*(?:pub\s+)?trait\s+(\w+)", "class"),
            (r"^\s*(?:pub\s+)?fn\s+(\w+)", "function"),
        ],
        "kotlin": [
            (r"^\s*(?:data\s+|abstract\s+|sealed\s+)?class\s+(\w+)", "class"),
            (r"^\s*(?:data\s+)?object\s+(\w+)", "class"),
            (r"^\s*interface\s+(\w+)", "class"),
            (r"^\s*(?:override\s+)?(?:suspend\s+)?fun\s+(\w+)", "function"),
        ],
        "typescript": [
            (r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", "class"),
            (r"^\s*(?:export\s+)?interface\s+(\w+)", "class"),
            (r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function"),
            (r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(", "function"),
            (r"^\s+(?:static\s+)?(?:public\s+|private\s+|protected\s+)?(?:async\s+)?(?:get\s+|set\s+)?(\w+)\s*\([^)]*\)\s*[:{]", "function"),
        ],
        "c_sharp": [
            (r"^\s*(?:public|private|protected|internal)?\s*(?:abstract\s+|sealed\s+|static\s+)?class\s+(\w+)", "class"),
            (r"^\s*(?:public|private|protected|internal)?\s*interface\s+(\w+)", "class"),
            (r"^\s*(?:public|private|protected|internal|static|virtual|override|async)?\s*\w+\s+(\w+)\s*\(", "function"),
        ],
    }

    GENERIC_DECLARATION_TYPES = {
        "class_definition", "class_declaration", "class_specifier",
        "interface_declaration", "interface_definition", "struct_item",
        "struct_specifier", "enum_item", "enum_declaration", "trait_item",
        "trait_definition", "module", "module_definition", "namespace_definition",
        "namespace_declaration", "object_definition", "type_declaration",
        "function_definition", "function_definition_statement", "function_declaration",
        "function_item", "method_definition", "method_declaration", "method", "constructor",
    }

    GENERIC_NAME_TYPES = {
        "identifier", "type_identifier", "property_identifier", "field_identifier",
        "constant", "name", "name_identifier", "shorthand_property_identifier",
    }

    def _extract_regex_ast(self, file_id: int, file_rel_path: str, content: str, lang_name: str, conn=None):
        """Regex-based AST extraction fallback when tree_sitter_languages is unavailable."""
        import re
        patterns = self._REGEX_AST_PATTERNS.get(lang_name)
        if not patterns:
            return
        lines = content.split("\n")
        for line_no, line in enumerate(lines, 1):
            for pattern, node_type in patterns:
                m = re.match(pattern, line)
                if m:
                    name = m.group(1)
                    if name and len(name) > 1:
                        self._safe_insert_ast_node(file_id, node_type, name, line_no, line_no, file_rel_path, conn=conn)
                    break

    def _extract_generic_tree_ast(self, file_id: int, file_rel_path: str, tree, content_bytes: bytes, conn=None):
        """Extract common declarations when a grammar has no custom query."""
        def walk(node):
            yield node
            for child in node.named_children:
                yield from walk(child)

        for node in walk(tree.root_node):
            if node.type not in self.GENERIC_DECLARATION_TYPES:
                continue

            name_node = None
            try:
                name_node = node.child_by_field_name("name")
            except Exception:
                pass
            if name_node is None:
                for child in node.named_children:
                    if child.type in self.GENERIC_NAME_TYPES:
                        name_node = child
                        break
            if name_node is None:
                continue

            name = content_bytes[name_node.start_byte:name_node.end_byte].decode(
                "utf-8", errors="ignore"
            ).strip()
            if not name:
                continue
            node_type = "class" if any(token in node.type for token in (
                "class", "interface", "struct", "enum", "trait", "module",
                "namespace", "object", "type",
            )) else "function"
            self._safe_insert_ast_node(
                file_id, node_type, name, node.start_point[0] + 1,
                node.end_point[0] + 1, file_rel_path, conn=conn
            )

    def _extract_and_store_ast(self, file_id: int, file_rel_path: str, content: str, conn=None):
        suffix = Path(file_rel_path).suffix.lower()
        if suffix not in self.EXTENSION_TO_LANG:
            if suffix == ".py":
                self._extract_python_native_ast(file_id, file_rel_path, content, conn=conn)
            return

        lang_name = self.EXTENSION_TO_LANG[suffix]

        if suffix == ".py":
            self._extract_python_native_ast(file_id, file_rel_path, content, conn=conn)
            return

        try:
            import tree_sitter_languages
        except ImportError:
            self._extract_regex_ast(file_id, file_rel_path, content, lang_name, conn=conn)
            return

        try:
            language = tree_sitter_languages.get_language(lang_name)
            parser = tree_sitter_languages.get_parser(lang_name)
            content_bytes = content.encode("utf-8")
            tree = parser.parse(content_bytes)

            query_scm = self.AST_QUERIES.get(lang_name)
            if not query_scm:
                self._extract_generic_tree_ast(
                    file_id, file_rel_path, tree, content_bytes, conn=conn
                )
                return

            query = language.query(query_scm)
            captures = query.captures(tree.root_node)

            for node, tag in captures:
                if tag.endswith(".name"):
                    continue

                node_type = "class" if tag == "class" else "function"
                name = self._ast_node_name(node, content_bytes)
                if not name:
                    continue

                start_line = node.start_point[0] + 1
                end_line = node.end_point[0] + 1
                self._safe_insert_ast_node(
                    file_id, node_type, name, start_line, end_line, file_rel_path, conn=conn
                )

        except Exception as e:
            logger.debug(f"Tree-sitter AST extraction failed for {file_rel_path} ({lang_name}): {e}")
            if suffix == ".py":
                self._extract_python_native_ast(file_id, file_rel_path, content, conn=conn)

    def _ast_node_name(self, node, content_bytes: bytes) -> str:
        name_types = {
            "identifier", "type_identifier", "constant", "scope_resolution",
            "name", "property_identifier", "field_identifier",
        }
        for child in node.children:
            if child.type in name_types:
                return content_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
        for child in node.named_children:
            if child.type in name_types or "name" in child.type:
                return content_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
        return ""

    def _extract_python_native_ast(self, file_id: int, file_rel_path: str, content: str, conn=None):
        import ast
        try:
            tree = ast.parse(content)
        except Exception as e:
            logger.debug(f"Native Python AST parse failed for {file_rel_path}: {e}")
            return

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node_type = "function"
            elif isinstance(node, ast.ClassDef):
                node_type = "class"
            else:
                continue

            start_line = getattr(node, "lineno", 1) or 1
            end_line = getattr(node, "end_lineno", None) or start_line
            self._safe_insert_ast_node(
                file_id, node_type, node.name, start_line, end_line, file_rel_path, conn=conn
            )

    def _safe_insert_ast_node(self, file_id, node_type, name, start_line, end_line, file_rel_path, conn=None):
        try:
            ContextDB.insert_ast_node(file_id, node_type, name, start_line, end_line, conn=conn)
        except Exception as e:
            logger.warning(f"AST insert failed for {file_rel_path}:{start_line} ({name}): {e}")

    def _index_file(self, repo_id, repo_path, file_rel_path, embedder, conn, replace_generated=False):
        if MemoryBankDetector.should_skip_in_memory_dir(str(file_rel_path)):
            return None
        file_abs_path = repo_path / file_rel_path
        stat = file_abs_path.stat()
        if stat.st_size > 50_000_000:
            return None
        with open(file_abs_path, "r", encoding="utf-8", errors="ignore") as handle:
            content = handle.read()
        if "\x00" in content:
            return None
        language, is_memory_bank = self._detect_language(str(file_rel_path))
        file_id = ContextDB.insert_file(
            repo_id, str(file_rel_path), language, is_memory_bank,
            int(stat.st_mtime_ns), datetime.now(timezone.utc).isoformat(), conn=conn,
        )
        if replace_generated:
            ContextDB.clear_file_generated_data(file_id, conn=conn)
        self._extract_and_store_ast(file_id, str(file_rel_path), content, conn=conn)
        chunk_count = 0
        for chunk_index, chunk_text in self.chunker.chunk_with_metadata(content):
            ContextDB.insert_chunk(file_id, chunk_index, chunk_text, embedder.embed_one(chunk_text), conn=conn)
            chunk_count += 1
        return {"chunks": chunk_count, "language": language, "is_memory_bank": is_memory_bank}

    def _generate_file_ast(self, repo_id, repo_path, file_rel_path, conn, replace_generated=False):
        if MemoryBankDetector.should_skip_in_memory_dir(str(file_rel_path)):
            return False
        file_abs_path = repo_path / file_rel_path
        stat = file_abs_path.stat()
        if stat.st_size > 5_000_000:
            return False
        with open(file_abs_path, "r", encoding="utf-8", errors="ignore") as handle:
            content = handle.read()
        language, is_memory_bank = self._detect_language(str(file_rel_path))
        if is_memory_bank:
            return False
        file_id = ContextDB.insert_file(
            repo_id, str(file_rel_path), language, False,
            int(stat.st_mtime_ns), datetime.now(timezone.utc).isoformat(), conn=conn,
        )
        if replace_generated:
            ContextDB.clear_file_ast_data(file_id, conn=conn)
        self._extract_and_store_ast(file_id, str(file_rel_path), content, conn=conn)
        return True

    def index_repository(self, repo_path: Path, repo_name: Optional[str] = None,
                         clear: bool = True,
                         job_progress_cb: Optional[Callable] = None) -> Dict[str, Any]:
        repo_path = Path(repo_path).resolve()
        if not repo_path.exists():
            raise FileNotFoundError(f"Path does not exist: {repo_path}")
        if not repo_path.is_dir():
            raise NotADirectoryError(f"Not a directory: {repo_path}")

        repo_name = repo_name or repo_path.name
        conn = get_connection()
        try:
            _set_status(repo_name, status="indexing", phase="Loading model", job_type="index",
                        progress=0, total=0, files_done=0, chunks_done=0,
                        current_file="", error=None)
            ContextDB.update_repo_status(repo_name, "indexing", conn=conn)
            embedder = self._get_embedder()

            repo = ContextDB.get_repo(repo_name, conn=conn)
            if not repo:
                repo = ContextDB.add_repo(repo_name, str(repo_path), conn=conn)
            repo_id = repo["id"]

            if clear:
                _set_status(repo_name, phase="Clearing old generated data")
                ContextDB.clear_index_data(repo_id, conn=conn)
                ContextDB.clear_ast_data(repo_id, conn=conn)

            if _is_cancelled(repo_name):
                raise _CancelledError(repo_name)
            _set_status(repo_name, phase="Scanning directory")
            walker = FileWalker(repo_path, tracked_only=True)
            files_to_index = list(walker.walk())
            total_files = len(files_to_index)

            _set_status(repo_name, phase="Reading files", total=total_files)
            files_indexed = 0
            chunks_indexed = 0
            memory_bank_count = 0
            lang_counts = {}
            errors = 0

            for file_rel_path in files_to_index:
                if _is_cancelled(repo_name):
                    raise _CancelledError(repo_name)

                try:
                    indexed = self._index_file(
                        repo_id, repo_path, file_rel_path, embedder, conn,
                        replace_generated=not clear,
                    )
                    if not indexed:
                        continue
                    if indexed["is_memory_bank"]:
                        memory_bank_count += 1
                    else:
                        language = indexed["language"]
                        lang_counts[language] = lang_counts.get(language, 0) + 1
                    files_indexed += 1
                    chunks_indexed += indexed["chunks"]

                    pct = int(files_indexed / total_files * 100) if total_files else 100
                    _set_status(repo_name, phase="Embedding", files_done=files_indexed,
                                chunks_done=chunks_indexed, progress=pct,
                                current_file=str(file_rel_path), errors=errors,
                                memory_bank=memory_bank_count, languages=dict(lang_counts))

                    if job_progress_cb and files_indexed % 5 == 0:
                        job_progress_cb(pct, "Embedding", str(file_rel_path))

                except _CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Error indexing {file_rel_path}: {e}")
                    errors += 1

            now = datetime.now(timezone.utc).isoformat()
            ContextDB.update_repo_status(repo_name, "indexed", indexed_at=now, conn=conn)
            _set_status(repo_name, status="indexed", phase="Complete", progress=100,
                        files_done=files_indexed, chunks_done=chunks_indexed,
                        current_file="", errors=errors)

            return {
                "repo_name": repo_name,
                "files_indexed": files_indexed,
                "chunks_indexed": chunks_indexed,
                "errors": errors,
            }

        except _CancelledError:
            ContextDB.update_repo_status(repo_name, "cancelled", conn=conn)
            _set_status(repo_name, status="cancelled", phase="Cancelled")
            return {"repo_name": repo_name, "cancelled": True}
        except Exception as e:
            ContextDB.update_repo_status(repo_name, "error", conn=conn)
            _set_status(repo_name, status="error", phase="Failed", error=str(e))
            raise
        finally:
            release_connection(conn)
            _clear_cancel(repo_name)
            _schedule_status_cleanup(repo_name)

    def generate_ast_for_repository(self, repo_path: Path, repo_name: Optional[str] = None,
                                    clear: bool = True,
                                    job_progress_cb: Optional[Callable] = None) -> Dict[str, Any]:
        repo_path = Path(repo_path).resolve()
        if not repo_path.exists():
            raise FileNotFoundError(f"Path does not exist: {repo_path}")
        if not repo_path.is_dir():
            raise NotADirectoryError(f"Not a directory: {repo_path}")

        repo_name = repo_name or repo_path.name
        conn = get_connection()
        try:
            _set_status(repo_name, status="indexing", phase="Preparing AST generation", job_type="ast",
                        progress=0, total=0, files_done=0, ast_nodes_done=0,
                        current_file="", error=None)

            repo = ContextDB.get_repo(repo_name, conn=conn)
            if not repo:
                repo = ContextDB.add_repo(repo_name, str(repo_path), conn=conn)
            repo_id = repo["id"]

            if clear:
                _set_status(repo_name, phase="Clearing old AST data")
                ContextDB.clear_ast_data(repo_id, conn=conn)

            if _is_cancelled(repo_name):
                raise _CancelledError(repo_name)
            _set_status(repo_name, phase="Scanning directory")
            walker = FileWalker(repo_path, tracked_only=True)
            files_to_index = list(walker.walk())
            total_files = len(files_to_index)

            _set_status(repo_name, phase="Generating AST", total=total_files)
            files_indexed = 0
            errors = 0

            for file_rel_path in files_to_index:
                if _is_cancelled(repo_name):
                    raise _CancelledError(repo_name)

                try:
                    processed = self._generate_file_ast(
                        repo_id, repo_path, file_rel_path, conn,
                        replace_generated=not clear,
                    )
                    if not processed:
                        continue
                    files_indexed += 1

                    pct = int(files_indexed / total_files * 100) if total_files else 100
                    _set_status(repo_name, phase="Extracting", files_done=files_indexed,
                                progress=pct, current_file=str(file_rel_path), errors=errors)

                    if job_progress_cb and files_indexed % 5 == 0:
                        job_progress_cb(pct, "Extracting", str(file_rel_path))

                except _CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Error extracting AST for {file_rel_path}: {e}")
                    errors += 1

            _set_status(repo_name, status="indexed", phase="Complete", progress=100)
            ContextDB.update_repo_status(repo_name, "ast_only", conn=conn)
            return {"repo_name": repo_name, "files_processed": files_indexed, "errors": errors}

        except _CancelledError:
            _set_status(repo_name, status="cancelled", phase="Cancelled")
            return {"repo_name": repo_name, "cancelled": True}
        except Exception as e:
            _set_status(repo_name, status="error", phase="Failed", error=str(e))
            raise
        finally:
            release_connection(conn)
            _clear_cancel(repo_name)
            _schedule_status_cleanup(repo_name)

    def index_in_background(self, repo_path: Path, repo_name: Optional[str] = None):
        def _run():
            try:
                self.index_repository(repo_path, repo_name)
            except Exception as e:
                logger.error(f"Background indexing failed: {e}")
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t

    def generate_ast_in_background(self, repo_path: Path, repo_name: Optional[str] = None):
        def _run():
            try:
                self.generate_ast_for_repository(repo_path, repo_name)
            except Exception as e:
                logger.error(f"Background AST generation failed: {e}")
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t

    def delete_repository(self, repo_name: str) -> bool:
        return ContextDB.delete_repo(repo_name)
