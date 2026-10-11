"""Bounded graph reconciliation; invoked only by the durable maintenance runner."""

import json
from collections import defaultdict

from knowledge.contemplate_matching import CandidateIndex, root_domains, stable_order, text_guard, workspaces

MAX_AFFECTED_NODES = 20


class Contemplation:
    def __init__(self, cur, matcher, summary: dict, before: dict, now: str, threshold: float) -> None:
        self.cur, self.matcher, self.summary = cur, matcher, summary
        self.now, self.threshold = now, threshold
        self.nodes = {key: dict(row) for key, row in before["kg_nodes"].items()}
        self.adjacency = defaultdict(set)
        for edge in before["kg_edges"].values():
            self.adjacency[edge["source_id"]].add(edge["target_id"])
            self.adjacency[edge["target_id"]].add(edge["source_id"])
        self.domains = {}
        self.affected = set()
        self.deferred = set()
        self.changed_survivors = set()
        self.actions = []
        self.index = CandidateIndex()
        for node in self.nodes.values():
            if node["status"] == "committed":
                self.index.add(node)

    def compatible(self, left: dict, right: dict) -> bool:
        from knowledge.maintenance import _metadata
        if left["node_type"] != right["node_type"] or left["node_type"] == "domain":
            return False
        for node in (left, right):
            key = node["node_id"]
            if key not in self.domains:
                self.domains[key] = root_domains(key, self.nodes, self.adjacency)
        a, b = self.domains[left["node_id"]], self.domains[right["node_id"]]
        if a or b:
            return bool(a and a == b)
        return bool(workspaces(_metadata(left["metadata"])) & workspaces(_metadata(right["metadata"])))

    def record_deferred(self, node: dict, canonical: dict, reason: str, score: float | None = None) -> None:
        self.deferred.add(node["node_id"])
        self.summary["deferred_operations"] += 1
        self.actions.append({"operation": "deferred", "node_ids": [canonical["node_id"], node["node_id"]],
                             "reason": reason, "cosine_similarity": score})

    def merge(self, canonical: dict, duplicate: dict, *, contradiction: bool = False, score: float | None = None) -> bool:
        from knowledge.maintenance import _absorb
        ids = {canonical["node_id"], duplicate["node_id"]}
        if len(self.affected | ids) > MAX_AFFECTED_NODES:
            self.summary["limit_reached"] = True
            self.record_deferred(duplicate, canonical, "affected_node_limit", score)
            return False
        self.index.remove(canonical)
        self.index.remove(duplicate)
        _absorb(self.cur, canonical, duplicate, now=self.now, contradiction=contradiction)
        self.cur.execute("SELECT * FROM kg_nodes WHERE node_id = %s", (canonical["node_id"],))
        survivor = dict(self.cur.fetchone())
        self.nodes[canonical["node_id"]] = survivor
        del self.nodes[duplicate["node_id"]]
        self.index.add(survivor)
        self.affected.update(ids)
        if len(self.affected) == MAX_AFFECTED_NODES:
            self.summary["limit_reached"] = True
        self.changed_survivors.add(survivor["node_id"])
        self.summary["duplicates_merged"] += 1
        self.summary["contradictions_resolved"] += int(contradiction)
        self.actions.append({"operation": "supersede" if contradiction else "merge", "node_ids": sorted(ids),
                             "survivor_id": survivor["node_id"], "absorbed_id": duplicate["node_id"],
                             "reason": "explicit_supersession" if contradiction else "cosine_and_strict_text_guard",
                             "cosine_similarity": score})
        return True

    def match(self, node: dict) -> bool:
        if node["node_type"] == "domain":
            return False
        candidates = sorted((self.nodes[key] for key in self.index.candidates(node)
                             if key in self.nodes), key=stable_order)
        for canonical in candidates:
            if stable_order(canonical) >= stable_order(node) and node["status"] == "committed":
                continue
            if not self.compatible(canonical, node):
                self.record_deferred(node, canonical, "incompatible_scope_or_type")
                continue
            if not text_guard(canonical, node):
                continue
            try:
                score = self.matcher.score(canonical, node, refresh=canonical["node_id"] in self.changed_survivors)
            except ValueError as exc:
                self.record_deferred(node, canonical, str(exc))
                continue
            if score < self.threshold:
                self.record_deferred(node, canonical, "below_similarity_threshold", score)
                continue
            return self.merge(canonical, node, score=score)
        return False

    def promote(self, node: dict) -> None:
        from knowledge.maintenance import _metadata
        meta = _metadata(node["metadata"])
        meta["cluster"] = meta.get("cluster") or f"{node['node_type']}-knowledge"
        meta["promoted_at"] = self.now
        self.cur.execute(
            "UPDATE kg_nodes SET status = 'committed', metadata = %s, updated_at = %s WHERE node_id = %s",
            (json.dumps(meta), self.now, node["node_id"]),
        )
        node.update(status="committed", metadata=json.dumps(meta), updated_at=self.now)
        self.index.add(node)
        self.summary["nodes_promoted"] += 1
        self.summary["clusters_assigned"] += 1
        self.actions.append({"operation": "promote", "node_ids": [node["node_id"]], "reason": "staged_knowledge"})

    def reconcile(self) -> None:
        from knowledge.maintenance import _metadata
        staged_ids = [node["node_id"] for node in sorted(self.nodes.values(), key=stable_order)
                      if node["status"] == "staged"]
        for node_id in staged_ids:
            node = self.nodes[node_id]
            meta = _metadata(node["metadata"])
            supersedes = str(meta.get("supersedes_node_id") or meta.get("contradicts_node_id") or "")
            if supersedes:
                canonical = self.nodes.get(supersedes)
                if canonical and canonical["status"] == "committed" and self.compatible(canonical, node):
                    self.merge(canonical, node, contradiction=True)
                else:
                    self.deferred.add(node_id)
                    self.summary["deferred_operations"] += 1
                    self.actions.append({"operation": "deferred", "node_ids": [node_id, supersedes],
                                         "reason": "supersession_target_missing_or_incompatible"})
                continue
            if not self.match(node) and node_id not in self.deferred:
                self.promote(node)

        committed_ids = [node["node_id"] for node in sorted(self.nodes.values(), key=stable_order)
                         if node["status"] == "committed"]
        for node_id in committed_ids:
            if node_id in self.nodes and node_id not in self.deferred:
                self.match(self.nodes[node_id])

        self.cur.execute(
            """UPDATE kg_nodes SET metadata = jsonb_set(metadata::jsonb, '{is_active}', 'false'::jsonb)::text,
               updated_at = %s WHERE status = 'committed'
               AND COALESCE((metadata::jsonb ->> 'expires_at')::timestamptz, 'infinity'::timestamptz) <= CURRENT_TIMESTAMP
               AND COALESCE(metadata::jsonb ->> 'is_active', 'true') <> 'false' RETURNING node_id""", (self.now,),
        )
        expired = [row["node_id"] for row in self.cur.fetchall()]
        self.summary["nodes_expired"] = len(expired)
        if expired:
            self.actions.append({"operation": "expire", "node_ids": expired, "reason": "expires_at_elapsed"})
        self.cur.execute("DELETE FROM kg_edges WHERE source_id = target_id RETURNING edge_id")
        pruned = [row["edge_id"] for row in self.cur.fetchall()]
        self.summary["edges_pruned"] = len(pruned)
        if pruned:
            self.actions.append({"operation": "prune_edges", "edge_ids": pruned, "reason": "self_edges"})
        self.summary["affected_node_ids"] = sorted(self.affected)
        self.summary["affected_node_count"] = len(self.affected)
