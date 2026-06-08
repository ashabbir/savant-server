import pytest

from abilities.resolver import Resolver
from abilities.store import Block


class _Store:
    def __init__(self):
        self.blocks_by_id = {}
        self._tag_hits = []
        self._repo = (None, None)

    def find_persona(self, name_or_id):
        if name_or_id == "eng":
            return self.blocks_by_id.get("persona.eng")
        return self.blocks_by_id.get(name_or_id)

    def find_repo_fuzzy(self, repo_id):
        return self._repo

    def blocks_with_tags(self, _tags, allowed_types=None):
        if not allowed_types:
            return self._tag_hits
        return [(b, info) for b, info in self._tag_hits if b.type in allowed_types]

    def get(self, block_id):
        return self.blocks_by_id.get(block_id)


def _b(
    bid,
    btype,
    body,
    priority=10,
    tags=None,
    includes=None,
):
    return Block(
        id=bid,
        type=btype,
        tags=tags or [],
        priority=priority,
        includes=includes or [],
        body=body,
    )


def test_resolve_unknown_persona_raises():
    s = _Store()
    r = Resolver(s)
    with pytest.raises(RuntimeError, match="Unknown persona"):
        r.resolve("missing", tags=[])


def test_resolve_composes_sections_and_manifest_with_trace():
    s = _Store()
    s.blocks_by_id = {
        "persona.eng": _b("persona.eng", "persona", "Persona body", priority=100, includes=["rule.base"]),
        "rule.base": _b("rule.base", "rule", "Base rule", priority=50),
        "repo.alpha": _b("repo.alpha", "repo", "Repo body", priority=90, tags=["python"], includes=["policy.sec"]),
        "policy.sec": _b("policy.sec", "policy", "Security policy", priority=40),
        "rule.tagged": _b("rule.tagged", "rule", "Tagged rule", priority=60, tags=["python"]),
        "style.clean": _b("style.clean", "style", "Style body", priority=20, tags=["python"]),
    }
    s._repo = (s.blocks_by_id["repo.alpha"], {"match_type": "exact", "score": 1.0})
    s._tag_hits = [
        (s.blocks_by_id["rule.tagged"], {"match_type": "exact"}),
        (s.blocks_by_id["style.clean"], {"match_type": "fuzzy"}),
    ]

    r = Resolver(s)
    out = r.resolve("eng", tags=["python"], repo_id="alpha", include_trace=True)

    assert out["persona"] == "Persona body"
    assert out["repo"] == "Repo body"
    assert "Base rule" in out["rules"]
    assert "Tagged rule" in out["rules"]
    assert "Security policy" in out["policies"]
    assert "Style body" in out["policies"]
    assert out["manifest"]["applied"]["persona"] == "persona.eng"
    assert out["manifest"]["applied"]["repo"] == "repo.alpha"
    assert len(out["manifest"]["hash"]) == 64
    assert out["trace"]
    assert "# Persona" in out["prompt"]
    assert "# Repo Constraints" in out["prompt"]
    assert "# Rules" in out["prompt"]
    assert "# Policies & Style" in out["prompt"]


def test_resolve_ignores_non_repo_repo_match_and_handles_empty_sections():
    s = _Store()
    s.blocks_by_id = {
        "persona.eng": _b("persona.eng", "persona", "P", priority=100),
        "rule.one": _b("rule.one", "rule", "R", priority=10),
    }
    # wrong type returned from repo finder: should be ignored
    s._repo = (_b("rule.notrepo", "rule", "X"), {"match_type": "fuzzy"})
    s._tag_hits = [(s.blocks_by_id["rule.one"], {"match_type": "exact"})]

    r = Resolver(s)
    out = r.resolve("eng", tags=["x"], repo_id="anything")

    assert out["repo"] == ""
    assert out["policies"] == []
    assert out["rules"] == ["R"]
    assert out["manifest"]["applied"]["repo"] == ""


def test_expand_includes_unknown_include_raises():
    s = _Store()
    persona = _b("persona.eng", "persona", "P", includes=["rule.missing"])
    s.blocks_by_id = {"persona.eng": persona}

    r = Resolver(s)
    with pytest.raises(RuntimeError, match="Unknown include"):
        r.resolve("eng", tags=[])


def test_render_section_empty_and_non_empty():
    assert Resolver._render_section("Rules", []) == ""
    sec = Resolver._render_section("Rules", [_b("rule.a", "rule", "A", priority=1)])
    assert sec.startswith("# Rules")
    assert "rule.a" in sec
