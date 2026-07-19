from context.analysis import AnalysisTarget, analyze_code


def _complexity(source):
    result = analyze_code(
        content_before=source,
        target=AnalysisTarget(path="sample.py"),
    )
    return result["after"]["complexity"]


def test_python_branching_increases_ast_complexity_at_equal_line_count():
    straight_line = """\
def choose(value):
    result = value
    result += 1
    return result
"""
    branching = """\
def choose(value):
    if value:
        return 1
    return 0
"""

    assert _complexity(branching) > _complexity(straight_line)


def test_boolean_branches_contribute_to_python_ast_complexity():
    simple = "def allowed(a, b, c):\n    return a\n"
    compound = "def allowed(a, b, c):\n    return a and b and c\n"

    assert _complexity(compound) >= _complexity(simple) + 2


def test_large_python_function_uses_ast_span_for_bloat_finding():
    body = "\n".join(f"    value_{index} = {index}" for index in range(125))
    source = f"def oversized():\n{body}\n    return value_124\n"

    result = analyze_code(
        content_before=source,
        target=AnalysisTarget(path="large.py"),
    )

    findings = result["after"]["findings"]
    assert any(
        finding["rule_id"] == "large_block_bloat" and "oversized spans 127 lines" in finding["detail"]
        for finding in findings
    )


def test_valid_python_branches_do_not_report_empty_or_unreachable_code():
    source = """\
def sample(value):
    if value:
        return value
    return None
"""

    result = analyze_code(
        content_before="",
        content_after=source,
        target=AnalysisTarget(path="sample.py", name="sample", node_type="function"),
        target_missing_is_new=True,
    )

    rule_ids = {finding["rule_id"] for finding in result["after"]["findings"]}
    assert "empty_block" not in rule_ids
    assert "unreachable_code" not in rule_ids


def test_python_pass_only_block_is_reported_as_empty():
    source = """\
def sample(value):
    if value:
        pass
    return None
"""

    result = analyze_code(content_before=source, target=AnalysisTarget(path="sample.py"))

    assert any(finding["rule_id"] == "empty_block" for finding in result["after"]["findings"])


def test_python_statement_after_return_in_same_block_is_unreachable():
    source = """\
def sample():
    return 1
    print("never")
"""

    result = analyze_code(content_before=source, target=AnalysisTarget(path="sample.py"))

    assert any(
        finding["rule_id"] == "unreachable_code" and finding["line"] == 3
        for finding in result["after"]["findings"]
    )


def test_every_statement_after_terminal_in_same_block_is_unreachable():
    source = """\
def sample(items):
    for item in items:
        if item is None:
            continue
            print("never")
            item = "also never"
        print(item)
"""

    result = analyze_code(content_before=source, target=AnalysisTarget(path="sample.py"))

    unreachable_lines = {
        finding["line"]
        for finding in result["after"]["findings"]
        if finding["rule_id"] == "unreachable_code"
    }
    assert unreachable_lines == {5, 6}


def test_nested_guard_clause_does_not_make_parent_block_unreachable():
    source = """\
def sample(value):
    if not value:
        raise ValueError("value required")
    normalized = value.strip()
    return normalized
"""

    result = analyze_code(content_before=source, target=AnalysisTarget(path="sample.py"))

    assert not any(
        finding["rule_id"] == "unreachable_code"
        for finding in result["after"]["findings"]
    )


def test_unchanged_analysis_reports_unchanged_status():
    source = "def sample() -> int:\n    return 1\n"

    result = analyze_code(
        content_before=source,
        target=AnalysisTarget(path="sample.py", name="sample", node_type="function"),
    )

    assert result["summary"]["status"] == "unchanged"
