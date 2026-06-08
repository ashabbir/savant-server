"""
Tests for JavaScript syntax integrity after modularization.
Validates extracted .js files under savant/static/js/ and the updated index.html.
"""

import glob
import os
import re
import subprocess

STATIC_JS_DIR = os.path.join(os.path.dirname(__file__), '..', 'static', 'js')
TEMPLATE = os.path.join(os.path.dirname(__file__), '..', 'templates', 'index.html')


def _js_files():
    """Return sorted list of .js file paths in static/js/."""
    return sorted(glob.glob(os.path.join(STATIC_JS_DIR, '*.js')))


def _read_template():
    with open(TEMPLATE) as f:
        return f.read()


def _extract_inline_scripts(html):
    """Return list of (start_line, text, char_count) for inline <script> blocks."""
    blocks = []
    pattern = re.compile(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)
    for m in pattern.finditer(html):
        start_line = html[:m.start()].count('\n') + 1
        text = m.group(1)
        blocks.append((start_line, text, len(text)))
    return blocks


# ── node --check on every .js file ───────────────────────────────────────────

def test_all_js_files_pass_node_check():
    """For every .js file in savant/static/js/, run node --check and assert exit code 0."""
    js_files = _js_files()
    assert len(js_files) > 0, "No .js files found in static/js/"
    errors = []
    for path in js_files:
        result = subprocess.run(['node', '--check', path], capture_output=True, text=True)
        if result.returncode != 0:
            errors.append(f"{os.path.basename(path)}:\n{result.stderr.strip()}")
    assert not errors, "JS syntax errors found by node --check:\n" + "\n\n".join(errors)


# ── Orphaned statements ──────────────────────────────────────────────────────

def _check_orphaned(js_text, keyword_pattern, filename=''):
    """Check for keyword at brace-depth 0 in JS text. Returns list of offending lines."""
    offenders = []
    depth = 0
    for i, line in enumerate(js_text.split('\n'), 1):
        stripped = line.strip()
        if stripped.startswith('//') or stripped.startswith('*') or stripped.startswith('/*'):
            continue
        # Remove template literals' ${} to avoid false brace counts
        clean = re.sub(r'\$\{', '', stripped)
        opens = clean.count('{')
        closes = clean.count('}')
        prev_depth = depth
        depth += opens - closes
        if prev_depth == 0 and re.match(keyword_pattern, stripped):
            offenders.append(f"{filename}:{i}: {stripped[:80]}")
    return offenders


def test_no_orphaned_return_statements():
    """Check all .js files for return at brace-depth 0."""
    all_offenders = []
    for path in _js_files():
        with open(path) as f:
            js = f.read()
        offenders = _check_orphaned(js, r'^return\b', os.path.basename(path))
        all_offenders.extend(offenders)
    assert not all_offenders, "Orphaned return statements:\n" + "\n".join(all_offenders)


def test_no_orphaned_await_statements():
    """Check all .js files for await at brace-depth 0."""
    all_offenders = []
    for path in _js_files():
        with open(path) as f:
            js = f.read()
        offenders = _check_orphaned(js, r'^await\s', os.path.basename(path))
        all_offenders.extend(offenders)
    assert not all_offenders, "Orphaned await statements:\n" + "\n".join(all_offenders)


# ── Script tags reference existing files ─────────────────────────────────────

def test_script_tags_reference_existing_files():
    """Parse index.html for <script src="/static/js/..."> and verify each file exists."""
    html = _read_template()
    pattern = re.compile(r'<script\s+src="/static/js/([^"]+)"')
    refs = pattern.findall(html)
    assert len(refs) > 0, "No <script src='/static/js/...'> tags found in index.html"
    missing = []
    for filename in refs:
        path = os.path.join(STATIC_JS_DIR, filename)
        if not os.path.isfile(path):
            missing.append(filename)
    assert not missing, f"Script tags reference missing files: {missing}"


# ── No large inline scripts ──────────────────────────────────────────────────

def test_no_large_inline_scripts():
    """Assert no inline <script> block exceeds 10000 chars."""
    html = _read_template()
    blocks = _extract_inline_scripts(html)
    large = []
    for start_line, text, chars in blocks:
        if chars > 10000:
            large.append(f"Line {start_line}: {chars} chars")
    assert not large, f"Inline script blocks exceed 10000 chars:\n" + "\n".join(large)


# ── All onclick functions declared ───────────────────────────────────────────

def test_all_functions_declared():
    """Extract all onclick='functionName(...)' from HTML, verify each function exists in .js files."""
    html = _read_template()

    # Collect all function names from onclick handlers
    # Patterns: onclick="funcName(...)" or onclick="funcName()" or onclick="event; funcName(...)"
    onclick_pattern = re.compile(r'onclick="([^"]*)"', re.IGNORECASE)
    func_call_pattern = re.compile(r'\b([a-zA-Z_]\w*)\s*\(')

    called_functions = set()
    for m in onclick_pattern.finditer(html):
        handler = m.group(1)
        for fc in func_call_pattern.finditer(handler):
            fname = fc.group(1)
            # Skip JS keywords, built-ins, and DOM method names
            if fname in ('if', 'else', 'return', 'throw', 'delete', 'void', 'typeof',
                         'instanceof', 'new', 'switch', 'case', 'for', 'while', 'do',
                         'try', 'catch', 'finally', 'with', 'in', 'of', 'let', 'var',
                         'const', 'class', 'extends', 'super', 'import', 'export',
                         'default', 'yield', 'async', 'await', 'debugger', 'break',
                         'continue', 'toggle', 'add', 'remove', 'replace',
                         'event', 'this', 'window', 'document', 'console', 'parseInt',
                         'parseFloat', 'setTimeout', 'clearTimeout', 'setInterval',
                         'clearInterval', 'alert', 'confirm', 'prompt', 'fetch',
                         'JSON', 'Math', 'Date', 'String', 'Number', 'Boolean',
                         'Array', 'Object', 'encodeURIComponent', 'decodeURIComponent',
                         'encodeURI', 'decodeURI', 'isNaN', 'isFinite', 'Error',
                         'localStorage', 'sessionStorage', 'history', 'location',
                         'navigator', 'performance', 'requestAnimationFrame',
                         'cancelAnimationFrame', 'requestIdleCallback', 'queueMicrotask',
                         'structuredClone', 'atob', 'btoa', 'URL', 'URLSearchParams',
                         'FormData', 'Headers', 'Request', 'Response', 'AbortController',
                         'TextEncoder', 'TextDecoder', 'Blob', 'File', 'FileReader',
                         'getComputedStyle', 'matchMedia', 'ResizeObserver',
                         'MutationObserver', 'IntersectionObserver', 'CustomEvent',
                         'dispatchEvent', 'addEventListener', 'removeEventListener',
                         'createElement', 'getElementById', 'querySelector',
                         'querySelectorAll', 'getElementsByClassName', 'getAttribute',
                         'setAttribute', 'removeAttribute', 'classList', 'style',
                         'closest', 'contains', 'matches', 'replaceWith',
                         'appendChild', 'removeChild', 'insertBefore', 'cloneNode',
                         'scrollTo', 'scrollIntoView', 'focus', 'blur', 'click',
                         'submit', 'reset', 'select', 'open', 'close', 'print',
                         'stop', 'toString', 'valueOf', 'hasOwnProperty', 'Symbol',
                         'Set', 'Map', 'WeakMap', 'WeakSet', 'Promise', 'Proxy',
                         'Reflect', 'BigInt', 'Intl', 'globalThis', 'undefined',
                         'NaN', 'Infinity', 'eval', 'Function', 'RegExp', 'TypeError',
                         'RangeError', 'SyntaxError', 'ReferenceError', 'URIError',
                         'AggregateError', 'Notification', 'stopPropagation', 'preventDefault'):
                continue
            called_functions.add(fname)

    # Collect all declared function names from .js files
    declared = set()
    func_decl_pattern = re.compile(r'(?:^|\s)(?:async\s+)?function\s+(\w+)\s*\(', re.MULTILINE)
    arrow_assign_pattern = re.compile(r'^(?:window\.)?(\w+)\s*=\s*(?:async\s+)?function', re.MULTILINE)
    window_assign_pattern = re.compile(r'window\.(\w+)\s*=\s*\(?(?:async\s+)?function', re.MULTILINE)

    for path in _js_files():
        with open(path) as f:
            content = f.read()
        for m in func_decl_pattern.finditer(content):
            declared.add(m.group(1))
        for m in arrow_assign_pattern.finditer(content):
            declared.add(m.group(1))
        for m in window_assign_pattern.finditer(content):
            declared.add(m.group(1))

    # Also check inline scripts in index.html
    blocks = _extract_inline_scripts(html)
    for _, text, _ in blocks:
        for m in func_decl_pattern.finditer(text):
            declared.add(m.group(1))
        for m in arrow_assign_pattern.finditer(text):
            declared.add(m.group(1))
        for m in window_assign_pattern.finditer(text):
            declared.add(m.group(1))

    missing = sorted(called_functions - declared)
    assert not missing, f"Functions called in onclick but not declared: {missing}"


# ── Template still exists ────────────────────────────────────────────────────

def test_template_exists():
    assert os.path.isfile(TEMPLATE), f"Template not found: {TEMPLATE}"


def test_js_directory_exists():
    assert os.path.isdir(STATIC_JS_DIR), f"JS directory not found: {STATIC_JS_DIR}"


# ── Inline script blocks still valid ─────────────────────────────────────────

def test_inline_scripts_pass_node_check():
    """Run node --check on remaining inline <script> blocks."""
    html = _read_template()
    blocks = _extract_inline_scripts(html)
    errors = []
    for i, (lineno, js, _) in enumerate(blocks):
        check_file = os.path.join(STATIC_JS_DIR, f'_check_inline_{i}.js')
        try:
            with open(check_file, 'w') as f:
                f.write(js)
            result = subprocess.run(['node', '--check', check_file], capture_output=True, text=True)
            if result.returncode != 0:
                errors.append(f"Inline block at line {lineno}:\n{result.stderr.strip()}")
        finally:
            if os.path.exists(check_file):
                os.unlink(check_file)
    assert not errors, "Inline script syntax errors:\n" + "\n\n".join(errors)


# ── Dev Debug Log Panel ──────────────────────────────────────────────────────

def test_dev_log_panel_html_exists():
    """The dev-log-panel div must exist in the template."""
    html = _read_template()
    assert 'id="dev-log-panel"' in html, "dev-log-panel div missing"
    assert 'id="dev-log-body"' in html, "dev-log-body div missing"
    assert 'id="dev-log-count"' in html, "dev-log-count span missing"


def test_dev_log_js_functions_exist():
    """Required JS functions for the dev log panel must be declared in .js files."""
    all_content = ''
    for path in _js_files():
        with open(path) as f:
            all_content += f.read()
    required = ['toggleDevLogs', 'openDevLogs', 'closeDevLogs', 'clearDevLogs',
                 '_appendDevLogEntry']
    missing = [fn for fn in required
               if f'function {fn}(' not in all_content and f'{fn} = function' not in all_content]
    assert not missing, f"Missing dev log functions: {missing}"
