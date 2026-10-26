"""v21 domain policy, strict similarity, bounded seeds and durable review regressions."""
import json

import numpy as np
import pytest
from db.knowledge_graph import KnowledgeGraphDB as KG
from knowledge.maintenance import run_contemplate_now
from tests.test_v21_indexing import query


@pytest.fixture(autouse=True)
def model(monkeypatch):
    from context.embeddings import EmbeddingModel
    class Model:
        def embed(self, texts):
            return np.array([[1.0, 0.0] for _ in texts])
        def embed_one(self, text):
            return [1.0, 0.0]
    monkeypatch.setattr(EmbeddingModel, 'get', lambda: Model())


def domain(threshold=0.70, title='Engineering'):
    metadata = {} if threshold is None else {'contemplate_similarity_threshold': threshold}
    node = KG.create_node({'title': title or 'temporary', 'node_type': 'domain', 'metadata': metadata})
    KG.commit_nodes([node['node_id']])
    if not title:
        query("UPDATE kg_nodes SET title='' WHERE node_id=%s", (node['node_id'],))
    return node


def node(title, root=None, kind='concept', **kwargs):
    result = KG.create_node({'title': title, 'content': kwargs.get('content', ''), 'node_type': kind,
                             'metadata': kwargs.get('metadata', {})})
    if root:
        KG.create_edge({'source_id': result['node_id'], 'target_id': root['node_id'], 'edge_type': 'part_of'})
    if kwargs.get('committed'):
        KG.commit_nodes([result['node_id']])
    return result


@pytest.mark.parametrize('score,expected', [(0.6999, 0), (0.70, 0), (0.7001, 1)])
def test_semantic_duplicates_use_strict_domain_threshold(monkeypatch, score, expected):
    from knowledge.contemplate_matching import SimilarityMatcher
    root = domain()
    node('Authentication tokens', root, committed=True)
    node('JWT credential validation', root)
    monkeypatch.setattr(SimilarityMatcher, 'score', lambda *args, **kw: score)
    assert run_contemplate_now('test')['duplicates_merged'] == expected


def test_exact_duplicates_do_not_need_embedding_service(monkeypatch):
    from context.embeddings import EmbeddingModel
    monkeypatch.setattr(EmbeddingModel, 'get', lambda: (_ for _ in ()).throw(RuntimeError('offline')))
    root = domain()
    node('Same exact title', root, content='One')
    node(' same EXACT title ', root, content='Two')
    assert run_contemplate_now('test')['duplicates_merged'] == 1


@pytest.mark.parametrize('threshold,title', [(None, 'Engineering'), (0.7, '')])
def test_missing_domain_policy_or_name_disables_merging(threshold, title):
    root = domain(threshold, title)
    node('Exact duplicate', root)
    node('Exact duplicate', root)
    result = run_contemplate_now('test')
    assert result['duplicates_merged'] == 0
    assert result['nodes_promoted'] == 2


def test_unscoped_workspace_duplicates_are_never_automatically_merged():
    for _ in range(2):
        node('Same title', metadata={'workspaces': ['shared']})
    assert run_contemplate_now('test')['duplicates_merged'] == 0


def test_seed_batch_is_newest_first_and_excludes_insights_and_domains():
    root = domain()
    for i in range(205):
        node(f'item {i}', root, content=f'unique {i}')
    insight = node('Related insight', root, kind='insight')
    newest = query("SELECT node_id FROM kg_nodes WHERE node_type NOT IN ('domain','insight') ORDER BY created_at DESC,node_id DESC LIMIT 200")
    result = run_contemplate_now('test')
    assert result['seeds_selected'] == 200
    assert result['affected_node_count'] <= 100
    assert result['nodes_promoted'] > 0
    assert not query("SELECT * FROM kg_nodes WHERE status='staged'")
    report = KG.get_maintenance_report(result['run_id'])
    seeds = report['configuration']['seed_ids']
    assert insight['node_id'] not in seeds and root['node_id'] not in seeds
    assert report['configuration']['domain_policies'][root['node_id']]['threshold'] == 0.70
    assert seeds == [row['node_id'] for row in newest]


def test_embedding_and_review_state_survive_separate_runs(monkeypatch):
    root = domain(1.0)
    node('First topic', root, committed=True)
    node('Second topic', root, committed=True)
    result = run_contemplate_now('test')
    assert result['duplicates_merged'] == 0
    assert query('SELECT * FROM kg_embedding_cache')
    assert len(query('SELECT * FROM kg_review_progress')) == 2
    again = run_contemplate_now('test')
    assert again['seeds_selected'] == 0
    query("UPDATE kg_nodes SET metadata=%s WHERE node_id=%s", (json.dumps({'contemplate_similarity_threshold': 0.6}), root['node_id']))
    assert run_contemplate_now('test')['duplicates_merged'] == 1


def test_affected_budget_is_100_and_deferred_nodes_are_published():
    root = domain()
    for i in range(102):
        node('Exact duplicate', root, content=f'evidence {i}')
    result = run_contemplate_now('test')
    assert result['duplicates_merged'] == 99
    assert result['affected_node_count'] == 100
    assert result['limit_reached'] is True
    assert not query("SELECT * FROM kg_nodes WHERE status='staged'")


def test_offline_model_after_exact_merge_defers_semantic_review(monkeypatch):
    from context.embeddings import EmbeddingModel
    monkeypatch.setattr(EmbeddingModel, 'get', lambda: (_ for _ in ()).throw(RuntimeError('offline')))
    root = domain()
    node('First topic', root, content='Evidence one', committed=True)
    node('Different wording', root, content='Other evidence')
    node('First topic', root, content='Evidence two')
    result = run_contemplate_now('test')
    assert result['status'] == 'success'
    assert result['duplicates_merged'] == 1
    assert result['deferred_operations'] > 0
