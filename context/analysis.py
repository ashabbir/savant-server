"""Deterministic source analysis helpers for context code tools."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, List, Dict


_PY_DEF_RE = re.compile(r"^\s*def\s+([A-Za-z_]\w*)\s*\(")
_JS_CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z_$][\w$]*)")
_JS_FN_RE = re.compile(r"^\s*(?:function\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\()")


class AnalysisRule(Protocol):
    def check(self, lines: List[str], path: str, findings: List[Dict[str, Any]]) -> None:
        ...


@dataclass
class AnalysisTarget:
    path: str
    name: str | None = None
    node_type: str | None = None


def _clamp(n: int, low: int = 0, high: int = 10_000) -> int:
    return max(low, min(high, n))


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + 1


def _push_finding(findings: list[dict[str, Any]], *, severity: str = "medium", category: str = "structural",
                  rule_id: str = "rule", path: str = "", line: int = 1, title: str = "Finding",
                  detail: str = "") -> None:
    findings.append({
        "severity": severity,
        "category": category,
        "rule_id": rule_id,
        "path": path,
        "line": line,
        "title": title,
        "detail": detail,
    })


def _detect_structural(lines: list[str], target_lines: list[str], path: str, findings: list[dict[str, Any]]) -> None:
    brace_depth = 0
    max_depth = 0
    max_depth_line = 1
    py_stack: list[int] = []
    for idx, raw in enumerate(lines, start=1):
        line = raw or ""
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#") or trimmed.startswith("//"):
            continue
        indent = len(line) - len(line.lstrip())
        while py_stack and indent <= py_stack[-1]:
            py_stack.pop()
        is_control = re.match(r"^(if|elif|for|while|try|except|catch|switch)\b", trimmed)
        if is_control:
            if trimmed.endswith(":"):
                py_stack.append(indent)
            depth = len(py_stack) + max(0, brace_depth)
            if depth > max_depth:
                max_depth = depth
                max_depth_line = idx
        opens = trimmed.count("{")
        closes = trimmed.count("}")
        brace_depth = max(0, brace_depth + opens - closes)
    if max_depth > 4:
        _push_finding(
            findings,
            severity="high",
            category="structural",
            rule_id="deep_nesting",
            path=path,
            line=max_depth_line,
            title="Deep control nesting",
            detail=f"Detected nesting depth {max_depth} (threshold: 4).",
        )

    file_nodes = []
    for line in target_lines:
        m = re.match(r"^\s*(class|def|function)\s+([A-Za-z_$][\w$]*)", line)
        if m:
            file_nodes.append((m.group(1), m.group(2), line))
        else:
            js_arrow = re.match(r"^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\(", line)
            if js_arrow:
                file_nodes.append(("function", js_arrow.group(1), line))

    for node_type, name, line in file_nodes:
        span = 1
        child_count = sum(1 for other_type, other_name, other_line in file_nodes if other_line != line and other_line.strip())
        is_class = node_type == "class"
        span_threshold = 220 if is_class else 120
        child_threshold = 12 if is_class else 8
        if span >= span_threshold or child_count >= child_threshold:
            _push_finding(
                findings,
                severity="high" if span >= span_threshold * 1.5 else "medium",
                category="structural",
                rule_id="large_block_bloat",
                path=path,
                line=1,
                title=f"{'Large class' if is_class else 'Large function'} bloat",
                detail=f"{name} spans {span} lines with {child_count} nested typed blocks.",
            )

    for idx, raw in enumerate(target_lines, start=1):
        line = raw or ""
        py = re.match(r"^\s*def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*:", line)
        js_fn = re.match(r"^\s*function\s+([A-Za-z_$][\w$]*)?\s*\(([^)]*)\)", line)
        js_arrow = re.match(r"^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\(([^)]*)\)\s*=>", line)
        hit = py or js_fn or js_arrow
        if not hit:
            continue
        params = [p.strip() for p in (hit.group(2) or "").split(",") if p.strip()]
        if len(params) > 5:
            _push_finding(
                findings,
                severity="medium" if len(params) <= 8 else "high",
                category="structural",
                rule_id="parameter_overload",
                path=path,
                line=idx,
                title="Parameter overload",
                detail=f"{hit.group(1) or 'Function'} has {len(params)} parameters.",
            )

    for idx, raw in enumerate(target_lines, start=1):
        line = (raw or "").strip()
        if not line:
            continue
        if re.search(r"(if|for|while|try|catch)\s*\([^)]*\)\s*\{\s*\}", line):
            _push_finding(findings, severity="low", category="structural", rule_id="empty_block", path=path, line=idx, title="Empty block", detail="Control block has an empty body.")
        if re.search(r"^(if|for|while|try|except)\b.*:\s*$", line):
            _push_finding(findings, severity="low", category="structural", rule_id="empty_block", path=path, line=idx, title="Empty block", detail="Python block appears empty/pass-only.")


def _detect_security(lines: list[str], path: str, findings: list[dict[str, Any]]) -> None:
    for idx, raw in enumerate(lines, start=1):
        line = (raw or "").strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if re.search(r"\b(API[_-]?KEY|PASSWORD|SECRET|TOKEN)\b\s*[:=]\s*['\"][^'\"]{6,}['\"]", line, re.I):
            _push_finding(findings, severity="high", category="security", rule_id="hardcoded_secret", path=path, line=idx, title="Hardcoded secret", detail="Literal secret-like value assigned in source.")
        if re.search(r"\b(eval|exec|os\.system)\s*\(", line):
            _push_finding(findings, severity="high", category="security", rule_id="insecure_call", path=path, line=idx, title="Insecure function call", detail="Use of eval/exec/os.system detected.")
        if re.search(r"\b(execute|query)\s*\(", line, re.I) and re.search(r'(f["\']|%|\.format\(|\+.*["\'])', line):
            _push_finding(findings, severity="high", category="security", rule_id="sql_injection_pattern", path=path, line=idx, title="Potential SQL injection pattern", detail="Query call appears to use string interpolation/concatenation.")


def _detect_modernization(lines: list[str], path: str, findings: list[dict[str, Any]]) -> None:
    for idx, raw in enumerate(lines, start=1):
        line = (raw or "").strip()
        if not line:
            continue
        if re.search(r"\b\w+\.append\(", line) and re.search(r"\bpd\b|\bpandas\b|dataframe|\bdf\.", line, re.I):
            _push_finding(findings, severity="low", category="modernization", rule_id="deprecated_append_api", path=path, line=idx, title="Deprecated append-style API usage", detail="Consider replacing append-style flows with concat-style batching.")


def _detect_style(lines: list[str], path: str, findings: list[dict[str, Any]]) -> None:
    for idx, raw in enumerate(lines, start=1):
        line = raw or ""
        if len(line.strip()) > 140:
            _push_finding(findings, severity="low", category="style", rule_id="long_line", path=path, line=idx, title="Long line", detail="Line exceeds 140 characters.")
        m = re.match(r"^\s*def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*:", line)
        if m and not re.search(r"->\s*[^:]+:", line):
            _push_finding(findings, severity="low", category="style", rule_id="missing_return_type_hint", path=path, line=idx, title="Missing return type hint", detail=f"{m.group(1)} has no return type annotation.")


def _detect_dead_code(lines: list[str], path: str, findings: list[dict[str, Any]]) -> None:
    for i in range(len(lines) - 1):
        curr = (lines[i] or "").strip()
        if not re.match(r"^(return|break|raise|throw)\b", curr):
            continue
        for j in range(i + 1, min(len(lines), i + 5)):
            nxt = (lines[j] or "").strip()
            if not nxt or nxt.startswith("#") or nxt.startswith("//") or nxt == "}":
                continue
            _push_finding(findings, severity="medium", category="dead_code", rule_id="unreachable_code", path=path, line=j + 1, title="Potential unreachable code", detail="Code appears after an early exit statement in the same block.")
            break


class StyleRule:
    def check(self, lines: List[str], path: str, findings: List[Dict[str, Any]]) -> None:
        _detect_style(lines, path, findings)

class SecurityRule:
    def check(self, lines: List[str], path: str, findings: List[Dict[str, Any]]) -> None:
        _detect_security(lines, path, findings)

class ModernizationRule:
    def check(self, lines: List[str], path: str, findings: List[Dict[str, Any]]) -> None:
        _detect_modernization(lines, path, findings)

class DeadCodeRule:
    def check(self, lines: List[str], path: str, findings: List[Dict[str, Any]]) -> None:
        _detect_dead_code(lines, path, findings)

class StructuralRule:
    def check(self, lines: List[str], path: str, findings: List[Dict[str, Any]]) -> None:
        # Note: _detect_structural takes lines twice
        _detect_structural(lines, lines, path, findings)

def _score_text(text: str) -> dict[str, Any]:
    if not text.strip():
        return {"complexity": 0, "findings": [], "line_count": 0}
    lines = text.splitlines()
    complexity = 1
    findings: list[dict[str, Any]] = []
    
    rules = [StructuralRule(), SecurityRule(), ModernizationRule(), StyleRule(), DeadCodeRule()]
    for rule in rules:
        rule.check(lines, "", findings)
    max_depth = 0
    depth = 0
    for raw in lines:
        line = (raw or "").rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        depth += stripped.count("{")
        max_depth = max(max_depth, depth)
        depth -= stripped.count("}")
        depth = max(depth, 0)
    complexity += _clamp(_line_count(text) // 25, 0, 8)
    return {
        "complexity": complexity,
        "findings": findings,
        "line_count": _line_count(text),
    }


def _pick_target_text(content: str, target: AnalysisTarget | None = None) -> dict[str, Any]:
    if not target or not target.name:
        return {"text": content, "target_found": False}

    lines = content.splitlines()
    if target.node_type == "class":
        start = None
        indent = None
        for idx, line in enumerate(lines):
            if re.match(rf"^\s*class\s+{re.escape(target.name)}\b", line):
                start = idx
                indent = len(line) - len(line.lstrip())
                break
        if start is None:
            return {"text": "", "target_found": False}
        end = len(lines)
        for idx in range(start + 1, len(lines)):
            cur = lines[idx]
            if not cur.strip():
                continue
            cur_indent = len(cur) - len(cur.lstrip())
            if cur_indent <= indent and re.match(r"^\s*(class|def|function)\b", cur):
                end = idx
                break
        return {"text": "\n".join(lines[start:end]), "target_found": True}

    matches = [
        idx for idx, line in enumerate(lines)
        if re.search(rf"\b{re.escape(target.name)}\b", line)
    ]
    if not matches:
        return {"text": "", "target_found": False}
    start = max(0, matches[0] - 2)
    end = min(len(lines), matches[0] + 40)
    return {"text": "\n".join(lines[start:end]), "target_found": True}


def _apply_unified_diff(original: str, diff_text: str) -> str:
    if not diff_text.strip():
        return original
    original_lines = original.splitlines()
    out: list[str] = []
    i = 0
    diff_lines = diff_text.splitlines()
    idx = 0
    while idx < len(diff_lines):
        line = diff_lines[idx]
        if not line.startswith("@@"):
            idx += 1
            continue
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if not m:
            idx += 1
            continue
        start = max(0, int(m.group(1)) - 1)
        while i < start and i < len(original_lines):
            out.append(original_lines[i])
            i += 1
        idx += 1
        while idx < len(diff_lines) and not diff_lines[idx].startswith("@@"):
            h = diff_lines[idx]
            if h.startswith(" "):
                if i < len(original_lines):
                    out.append(original_lines[i])
                    i += 1
            elif h.startswith("-"):
                i += 1
            elif h.startswith("+"):
                out.append(h[1:])
            idx += 1
    out.extend(original_lines[i:])
    return "\n".join(out)


def analyze_code(
    *,
    content_before: str = "",
    content_after: str | None = None,
    target: AnalysisTarget | None = None,
    diff: str | None = None,
    target_missing_is_new: bool = False,
) -> dict[str, Any]:
    before = content_before or ""
    if content_after is None and diff is not None:
        content_after = _apply_unified_diff(before, diff)
    if content_after is None:
        content_after = before

    before_pick = _pick_target_text(before, target)
    after_pick = _pick_target_text(content_after, target)
    if target_missing_is_new and not before_pick["target_found"]:
        before_pick = {"text": "", "target_found": False}
    if target_missing_is_new and not before.strip() and not after_pick["target_found"]:
        before_pick = {"text": "", "target_found": False}

    before_lines = before_pick["text"].splitlines()
    after_lines = after_pick["text"].splitlines()
    before_score = _score_text(before_pick["text"])
    after_score = _score_text(after_pick["text"])
    delta = after_score["complexity"] - before_score["complexity"]

    if before_pick["text"].strip():
        before_score["findings"] = []
        _detect_structural(before_lines, before_lines, target.path if target else "", before_score["findings"])
        _detect_security(before_lines, target.path if target else "", before_score["findings"])
        _detect_modernization(before_lines, target.path if target else "", before_score["findings"])
        _detect_style(before_lines, target.path if target else "", before_score["findings"])
        _detect_dead_code(before_lines, target.path if target else "", before_score["findings"])
        before_score["line_count"] = _line_count(before_pick["text"])

    if after_pick["text"].strip():
        after_score["findings"] = []
        _detect_structural(after_lines, after_lines, target.path if target else "", after_score["findings"])
        _detect_security(after_lines, target.path if target else "", after_score["findings"])
        _detect_modernization(after_lines, target.path if target else "", after_score["findings"])
        _detect_style(after_lines, target.path if target else "", after_score["findings"])
        _detect_dead_code(after_lines, target.path if target else "", after_score["findings"])
        after_score["line_count"] = _line_count(after_pick["text"])

    return {
        "target": {
            "path": target.path if target else "",
            "name": target.name if target else None,
            "node_type": target.node_type if target else None,
            "found_before": before_pick["target_found"],
            "found_after": after_pick["target_found"],
        },
        "before": before_score,
        "after": after_score,
        "delta": {
            "complexity": delta,
            "findings": len(after_score["findings"]) - len(before_score["findings"]),
            "line_count": after_score["line_count"] - before_score["line_count"],
        },
        "recommendation": {
            "summary": "If you have to refactor, be safe and go TDD.",
            "workflow": [
                "Write or update failing tests for the target behavior first.",
                "Make the safest minimal code change needed to satisfy the tests.",
                "Re-run analysis and tests before expanding the refactor.",
            ],
        },
        "summary": {
            "before_complexity": before_score["complexity"],
            "after_complexity": after_score["complexity"],
            "delta_complexity": delta,
            "before_findings": len(before_score["findings"]),
            "after_findings": len(after_score["findings"]),
            "status": "new" if not before_pick["target_found"] and after_pick["target_found"] else "updated",
        },
    }
