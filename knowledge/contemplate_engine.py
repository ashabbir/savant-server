"""Bounded graph reconciliation; invoked only by the durable maintenance runner."""

import json
import math
from collections import defaultdict

from knowledge.contemplate_matching import root_domains, stable_order, text_guard

MAX_AFFECTED_NODES = 100
MAX_SEEDS = 200
NEAREST_CANDIDATES = 20


class Contemplation:
    def __init__(self, cur, matcher, summary: dict, before: dict, now: str, threshold: float) -> None:
        self.cur, self.matcher, self.summary = cur, matcher, summary
        self.now, self.threshold = now, threshold
        self.nodes = {key: dict(row) for key, row in before["kg_nodes"].items()}
        self.adjacency = defaultdict(set)
        for edge in before["kg_edges"].values():
            self.adjacency[edge["source_id"]].add(edge["target_id"])
            self.adjacency[edge["target_id"]].add(edge["source_id"])
        self.edges = before["kg_edges"]
        self.domains = {}
        self.affected = set()
        self.deferred = set()
        self.changed_survivors = set()
        self.actions = []
        from db.contemplate import fingerprint
        identities = defaultdict(list)
        self.incident = defaultdict(list)
        for key, row in self.nodes.items():
            identities[row["node_type"]].append((key, row.get("updated_at"), row.get("title"), row.get("content")))
        self.candidate_identity = {kind: fingerprint(sorted(rows)) for kind, rows in identities.items()}
        for edge in self.edges.values():
            self.incident[edge["source_id"]].append(edge)
            self.incident[edge["target_id"]].append(edge)

    def scope_threshold(self, node: dict) -> tuple[tuple, float | None]:
        key = node["node_id"]
        if key in self.domains:
            return self.domains[key]
        result = self._scope_threshold(node)
        self.domains[key] = result
        return result

    def _scope_threshold(self, node: dict) -> tuple[tuple, float | None]:
        from knowledge.maintenance import _metadata
        domains = root_domains(node["node_id"], self.nodes, self.adjacency)
        policies = []
        for key in sorted(domains):
            root = self.nodes[key]
            title = " ".join(root.get("title", "").split()).casefold()
            raw = _metadata(root.get("metadata")).get("contemplate_similarity_threshold")
            if not title or raw is None or isinstance(raw, bool):
                return (), None
            try:
                threshold = self.threshold if raw == "default" else float(raw)
            except (TypeError, ValueError):
                return (), None
            if not math.isfinite(threshold) or not 0 <= threshold <= 1:
                return (), None
            policies.append((title, threshold))
        return tuple(sorted(policies)), max((value for _, value in policies), default=None)

    def compatible(self, left: dict, right: dict) -> bool:
        if left["node_type"] != right["node_type"] or left["node_type"] == "domain":
            return False
        a, threshold = self.scope_threshold(left)
        b, _ = self.scope_threshold(right)
        return bool(a and threshold is not None and a == b)

    def review_fingerprint(self, node: dict) -> str:
        from db.contemplate import fingerprint
        edges = self.incident[node["node_id"]]
        candidates = self.candidate_identity[node["node_type"]]
        return fingerprint([node, edges, candidates, self.scope_threshold(node), self.threshold,
                            self.matcher.model_identity, MAX_SEEDS, NEAREST_CANDIDATES, MAX_AFFECTED_NODES])

    def candidate_nodes(self, node: dict) -> list[tuple[dict, float | None]]:
        exact, scored, neighbors = [], [], []
        for candidate in self.nodes.values():
            if candidate["node_id"] == node["node_id"] or not self.compatible(candidate, node):
                continue
            if stable_order(candidate) >= stable_order(node):
                continue
            if text_guard(candidate, node):
                exact.append((candidate, None))
                continue
            try:
                score = self.matcher.score(candidate, node, refresh=candidate["node_id"] in self.changed_survivors)
                scored.append((candidate, score))
                if candidate["node_id"] in self.adjacency[node["node_id"]]:
                    neighbors.append((candidate, score))
            except ValueError as exc:
                self.record_deferred(node, candidate, str(exc))
        nearest = sorted(scored, key=lambda item: (-item[1], stable_order(item[0])))[:NEAREST_CANDIDATES]
        selected = {candidate["node_id"]: (candidate, score) for candidate, score in nearest + neighbors}
        return sorted(exact, key=lambda item: stable_order(item[0])) + list(selected.values())

    def record_deferred(self, node: dict, canonical: dict, reason: str, score: float | None = None) -> None:
        self.deferred.add(node["node_id"])
        self.summary["deferred_operations"] += 1
        self.actions.append({"operation": "deferred", "node_ids": [canonical["node_id"], node["node_id"]],
                             "reason": reason, "cosine_similarity": score})

    def merge(self, canonical: dict, duplicate: dict, *, contradiction: bool = False, score: float | None = None) -> bool:
        from knowledge.maintenance import _absorb
        if canonical["node_id"] == duplicate["node_id"]:
            raise ValueError("Cannot merge a node into itself")
        ids = {canonical["node_id"], duplicate["node_id"]}
        if len(self.affected | ids) > MAX_AFFECTED_NODES:
            self.summary["limit_reached"] = True
            self.record_deferred(duplicate, canonical, "affected_node_limit", score)
            return False
        _absorb(self.cur, canonical, duplicate, now=self.now, contradiction=contradiction)
        self.cur.execute("SELECT * FROM kg_nodes WHERE node_id = %s", (canonical["node_id"],))
        survivor = dict(self.cur.fetchone())
        self.nodes[canonical["node_id"]] = survivor
        del self.nodes[duplicate["node_id"]]
        self.cur.execute("SELECT * FROM kg_edges ORDER BY edge_id")
        self.edges = {row["edge_id"]: dict(row) for row in self.cur.fetchall()}
        self.adjacency.clear()
        self.domains.clear()
        for edge in self.edges.values():
            self.adjacency[edge["source_id"]].add(edge["target_id"])
            self.adjacency[edge["target_id"]].add(edge["source_id"])
        self.affected.update(ids)
        if len(self.affected) == MAX_AFFECTED_NODES:
            self.summary["limit_reached"] = True
        self.changed_survivors.add(survivor["node_id"])
        self.summary["duplicates_merged"] += 1
        self.summary["contradictions_resolved"] += int(contradiction)
        self.actions.append({"operation": "supersede" if contradiction else "merge", "node_ids": sorted(ids),
                             "survivor_id": survivor["node_id"], "absorbed_id": duplicate["node_id"],
                             "reason": "explicit_supersession" if contradiction else (
                                 "normalized_exact_match" if score is None else "cosine_similarity"),
                             "cosine_similarity": score})
        return True

    def match(self, node: dict) -> bool:
        if node["node_type"] == "domain":
            return False
        _, threshold = self.scope_threshold(node)
        for canonical, score in self.candidate_nodes(node):
            if not text_guard(canonical, node) and score <= threshold:
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
        self.summary["nodes_promoted"] += 1
        self.summary["clusters_assigned"] += 1
        self.actions.append({"operation": "promote", "node_ids": [node["node_id"]], "reason": "staged_knowledge"})

    def reconcile(self) -> None:
        from knowledge.maintenance import _metadata
        from db.contemplate import ContemplateDB
        reviews = ContemplateDB.reviews(self.cur)
        eligible = [node for node in self.nodes.values()
                    if node["node_type"] not in {"domain", "insight"} and
                    reviews.get(node["node_id"]) != self.review_fingerprint(node)]
        seeds = sorted(eligible, key=lambda node: (str(node["created_at"]), node["node_id"]), reverse=True)[:MAX_SEEDS]
        self.seed_ids = [node["node_id"] for node in seeds]
        self.summary["seeds_selected"] = len(seeds)
        reviewed = {}
        for seed in seeds:
            node_id = seed["node_id"]
            if node_id not in self.nodes:
                continue
            node = self.nodes[node_id]
            reviewed[node_id] = self.review_fingerprint(node)
            meta = _metadata(node["metadata"])
            supersedes = str(meta.get("supersedes_node_id") or meta.get("contradicts_node_id") or "")
            if supersedes:
                canonical = self.nodes.get(supersedes)
                if canonical and canonical["node_id"] != node_id and self.compatible(canonical, node):
                    self.merge(canonical, node, contradiction=True)
                else:
                    self.deferred.add(node_id)
                    self.summary["deferred_operations"] += 1
                    self.actions.append({"operation": "deferred", "node_ids": [node_id, supersedes],
                                         "reason": "supersession_target_missing_or_incompatible"})
            else:
                self.match(node)
        # Publishing staged knowledge is independent from the bounded merge review.
        for node in self.nodes.values():
            if node["status"] == "staged":
                self.promote(node)
        for node_id, value in reviewed.items():
            if node_id in self.nodes and node_id not in self.deferred:
                ContemplateDB.save_review(self.cur, node_id, value)

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
