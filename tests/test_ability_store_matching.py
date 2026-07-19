from abilities.store import AbilityStore, Block


def test_tag_matching_prefers_best_match_across_all_block_tags(tmp_path):
    store = AbilityStore(tmp_path)
    block = Block(
        id="rule.python",
        type="rule",
        tags=["py", "python_api"],
        priority=10,
        body="Use Python APIs safely.",
    )
    store.blocks_by_id[block.id] = block
    store.ids_by_tag = {tag: [block.id] for tag in block.tags}

    matches = store.blocks_with_tags(["python-api"], allowed_types={"rule"})

    assert len(matches) == 1
    _, detail = matches[0]
    assert detail["match_type"] == "exact"
    assert detail["score"] == 1.0
    assert detail["block_tag"] == "python_api"


def test_repo_fuzzy_does_not_match_blank_normalized_query(tmp_path):
    store = AbilityStore(tmp_path)
    block = Block(
        id="repo.savant-server",
        type="repo",
        name="Savant Server",
        tags=[],
        priority=10,
        body="Server rules.",
    )
    store.blocks_by_id[block.id] = block
    store.ids_by_type["repo"] = [block.id]

    match, detail = store.find_repo_fuzzy(" \t ")

    assert match is None
    assert detail is None


def test_ability_file_normalizes_scalar_includes_and_aliases(tmp_path):
    path = tmp_path / "repo.md"
    path.write_text(
        """---
id: repo.savant-server
type: repo
tags: python, flask
priority: 10
includes: rule.security
aliases: server
---
Server rules.
""",
        encoding="utf-8",
    )
    store = AbilityStore(tmp_path)

    store._load_file(path)

    block = store.blocks_by_id["repo.savant-server"]
    assert block.includes == ["rule.security"]
    assert block.aliases == ["server"]
    assert block.tags == ["python", "flask"]


def test_tag_matching_treats_scalar_query_as_one_tag(tmp_path):
    store = AbilityStore(tmp_path)
    block = Block(
        id="rule.python",
        type="rule",
        tags=["python-api"],
        priority=10,
        body="Python API rules.",
    )
    store.blocks_by_id[block.id] = block
    store.ids_by_tag = {"python-api": [block.id]}

    matches = store.blocks_with_tags("python-api", allowed_types={"rule"})

    assert matches[0][1]["query_tag"] == "python-api"
    assert matches[0][1]["match_type"] == "exact"
