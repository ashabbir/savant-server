"""Topic Modeling & User Activity Intelligence for Savant.

Extracts semantic topics, user profile archetype, and abilities affinity from:
  1. Search queries (mcp_query_log)
  2. Knowledge contributions (kg_nodes)
  3. Tool usage (mcp_tool_usage)
  4. Project / Repo usage (mcp_project_usage)
  5. Login activity (user_login_days, users.last_login_at)
  6. Abilities / Personas / Rules invocations
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from postgres_client import get_connection, release_connection

# Common stopwords + code/system noise
STOPWORDS: Set[str] = {
    "http", "https", "localhost", "users", "code", "src", "lib", "app",
    "ts", "js", "py", "json", "html", "css", "md", "com", "org", "net",
    "the", "and", "for", "with", "from", "this", "that", "are", "was",
    "were", "will", "have", "has", "had", "been", "can", "could", "should",
    "would", "not", "but", "all", "any", "one", "two", "three", "use",
    "using", "used", "new", "add", "added", "now", "via", "into", "out",
    "get", "set", "run", "runs", "test", "tests", "testing", "file", "files",
    "path", "item", "items", "data", "null", "undefined", "true", "false",
    "none", "todo", "fix", "check", "view", "component", "show", "shows",
    "default", "create", "created", "update", "updated", "delete", "deleted",
}

# Domain keyword mappings for clean topic labels and archetype inference
DOMAIN_TAXONOMY = {
    "Architecture & Systems": {"architect", "architecture", "system", "systems", "boundary", "boundaries", "service", "domain", "plane", "core", "infrastructure"},
    "Auth & Security": {"auth", "authentication", "authorization", "token", "tokens", "key", "keys", "permission", "permissions", "access", "role", "roles", "login"},
    "Knowledge Graph & RAG": {"knowledge", "graph", "kg", "rag", "reranker", "rerank", "precision", "embedding", "vector", "node", "nodes", "insight", "contemplation"},
    "Release & Delivery Workflow": {"release", "gate", "intake", "review", "reviewer", "delivery", "pipeline", "queue", "deploy", "deployment", "version", "build"},
    "Task & Backlog Management": {"task", "tasks", "board", "backlog", "flow", "inventory", "actionable", "sweep", "health", "ticket", "jira"},
    "Frontend & UI State": {"frontend", "ui", "renderer", "view", "modal", "tab", "tabs", "button", "screen", "panel", "olympus", "sanctum", "client"},
    "Backend API & Database": {"api", "rest", "endpoint", "route", "routes", "database", "postgres", "sql", "table", "schema", "query", "migration"},
    "Trading & Quantitative": {"trader", "trading", "product", "strategy", "portfolio", "metric", "signal", "market"},
    "Context Analysis & AST": {"context", "analyze", "analysis", "ast", "tree", "lossless", "chunk", "indexer", "search", "code"},
}


def clean_text(text: str) -> str:
    """Clean text by removing paths, hashes, punctuation, and noisy tokens."""
    if not text:
        return ""
    # Strip URLs and absolute paths
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"(/[a-zA-Z0-9_\-\.]+)+", " ", text)
    # Strip long numeric IDs or hashes
    text = re.sub(r"\b\d{5,}\b", " ", text)
    text = re.sub(r"\b[a-f0-9]{12,}\b", " ", text, flags=re.I)
    # Strip non-alphanumeric except spaces
    text = re.sub(r"[^\w\s]", " ", text)
    # Tokenize and filter
    words = [
        w.lower()
        for w in text.split()
        if len(w) > 2 and not w.isdigit() and w.lower() not in STOPWORDS
    ]
    return " ".join(words)


def synthesize_topic_label(terms: List[str]) -> str:
    """Generate a readable, thematic topic label based on top terms."""
    if not terms:
        return "General Development"

    term_set = set(t.lower() for t in terms)
    # Check for domain taxonomy match
    best_domain = None
    best_overlap = 0
    for domain, kws in DOMAIN_TAXONOMY.items():
        overlap = len(term_set.intersection(kws))
        if overlap > best_overlap:
            best_overlap = overlap
            best_domain = domain

    # Extract distinct non-domain keywords for the subtitle
    distinct_terms = [
        t.title() for t in terms[:3]
        if not best_domain or t.lower() not in DOMAIN_TAXONOMY.get(best_domain, set())
    ]
    if not distinct_terms:
        distinct_terms = [t.title() for t in terms[:2]]

    keywords_suffix = ", ".join(distinct_terms[:2])

    if best_domain:
        return f"{best_domain}: {keywords_suffix}" if keywords_suffix else best_domain

    title_terms = [t.title() for t in terms[:2]]
    return " & ".join(title_terms) if title_terms else "Activity Cluster"


class TopicModelingDB:
    """Topic Modeling & Activity Intelligence service."""

    @staticmethod
    def get_user_topic_model(user_id: str, days: int = 30, max_topics: int = 5) -> Dict[str, Any]:
        """Generate topic modeling, activity distribution, and ability profile for a user."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # 1. Fetch user metadata
                cur.execute(
                    """SELECT user_id, name, email, role, is_active, last_login_at
                       FROM users WHERE user_id = %s""",
                    (user_id,),
                )
                user_row = cur.fetchone()

                # 2. Fetch queries from mcp_query_log
                cur.execute(
                    """SELECT query, repo, tool_name, created_at, mcp_server
                       FROM mcp_query_log
                       WHERE user_id = %s AND created_at >= (now() - (%s || ' days')::interval)
                       ORDER BY created_at DESC""",
                    (user_id, days),
                )
                query_rows = cur.fetchall()

                # Fallback to all-time queries if window has none
                if not query_rows:
                    cur.execute(
                        """SELECT query, repo, tool_name, created_at, mcp_server
                           FROM mcp_query_log
                           WHERE user_id = %s
                           ORDER BY created_at DESC LIMIT 50""",
                        (user_id,),
                    )
                    query_rows = cur.fetchall()

                # 3. Fetch knowledge contributions from kg_nodes
                cur.execute(
                    """SELECT node_id, node_type, title, content, metadata, created_at
                       FROM kg_nodes
                       WHERE created_by = %s AND created_at >= (now() - (%s || ' days')::interval)
                       ORDER BY created_at DESC""",
                    (user_id, days),
                )
                kg_rows = cur.fetchall()

                # Fallback to all-time contributions if window has none
                if not kg_rows:
                    cur.execute(
                        """SELECT node_id, node_type, title, content, metadata, created_at
                           FROM kg_nodes
                           WHERE created_by = %s
                           ORDER BY created_at DESC LIMIT 200""",
                        (user_id,),
                    )
                    kg_rows = cur.fetchall()

                # 4. Fetch tool usages
                cur.execute(
                    """SELECT mcp_server, tool_name, SUM(calls) as calls, MAX(last_called_at) as last_called_at
                       FROM mcp_tool_usage
                       WHERE user_id = %s
                       GROUP BY mcp_server, tool_name
                       ORDER BY calls DESC""",
                    (user_id,),
                )
                tool_rows = cur.fetchall()

                # 5. Fetch project usages
                cur.execute(
                    """SELECT project_type, project, SUM(calls) as calls, MAX(last_used_at) as last_used_at
                       FROM mcp_project_usage
                       WHERE user_id = %s
                       GROUP BY project_type, project
                       ORDER BY calls DESC""",
                    (user_id,),
                )
                project_rows = cur.fetchall()

                # 6. Fetch logins
                cur.execute(
                    """SELECT day, logins FROM user_login_days
                       WHERE user_id = %s ORDER BY day DESC""",
                    (user_id,),
                )
                login_rows = cur.fetchall()
        finally:
            release_connection(conn)

        # -------------------------------------------------------------
        # Prepare Corpus for Topic Modeling
        # -------------------------------------------------------------
        docs: List[str] = []
        doc_meta: List[Dict[str, Any]] = []

        for q in query_rows:
            q_text = (q.get("query") or "").strip()
            cleaned = clean_text(q_text)
            if cleaned:
                docs.append(cleaned)
                doc_meta.append({
                    "source": "search",
                    "text": q_text,
                    "repo": q.get("repo") or "",
                    "tool": q.get("tool_name") or "",
                    "created_at": q["created_at"].isoformat() if q.get("created_at") else None,
                })

        for k in kg_rows:
            title = (k.get("title") or "").strip()
            content = (k.get("content") or "").strip()[:300]
            meta = (k.get("metadata") or "").strip()
            full_text = f"{title} {content} {meta}".strip()
            cleaned = clean_text(full_text)
            if cleaned:
                docs.append(cleaned)
                doc_meta.append({
                    "source": "contribution",
                    "text": title,
                    "node_type": k.get("node_type") or "insight",
                    "node_id": k.get("node_id"),
                    "created_at": k["created_at"].isoformat() if k.get("created_at") else None,
                })

        # -------------------------------------------------------------
        # Run Topic Modeling
        # -------------------------------------------------------------
        topics: List[Dict[str, Any]] = []

        if len(docs) >= 3:
            try:
                from sklearn.feature_extraction.text import TfidfVectorizer
                from sklearn.decomposition import NMF

                # Calculate optimal number of topics
                k = min(max_topics, max(2, len(docs) // 4))
                vectorizer = TfidfVectorizer(
                    max_df=0.85,
                    min_df=1,
                    ngram_range=(1, 2),
                    stop_words="english",
                )
                dtm = vectorizer.fit_transform(docs)
                feature_names = vectorizer.get_feature_names_out()

                import warnings
                from sklearn.exceptions import ConvergenceWarning

                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=ConvergenceWarning)
                    nmf = NMF(n_components=k, random_state=42, init="nndsvd", max_iter=500)
                    doc_topic_matrix = nmf.fit_transform(dtm)

                total_activity_docs = len(docs)
                for topic_idx, comp in enumerate(nmf.components_):
                    top_term_indices = comp.argsort()[:-8:-1]
                    top_terms = [feature_names[i] for i in top_term_indices]
                    term_weights = [
                        {"term": feature_names[i], "weight": round(float(comp[i]), 3)}
                        for i in top_term_indices
                    ]

                    # Assign docs to this topic
                    assigned_indices = [
                        i for i in range(len(docs))
                        if doc_topic_matrix[i].argmax() == topic_idx
                    ]

                    assigned_docs = [doc_meta[i] for i in assigned_indices]
                    search_items = [d for d in assigned_docs if d["source"] == "search"]
                    contrib_items = [d for d in assigned_docs if d["source"] == "contribution"]

                    weight_pct = round((len(assigned_indices) / total_activity_docs) * 100, 1) if total_activity_docs > 0 else 0

                    label = synthesize_topic_label(top_terms)

                    # Build samples
                    sample_searches = [{"text": s["text"], "tool": s["tool"]} for s in search_items[:3]]
                    sample_contribs = [{"title": c["text"], "node_type": c["node_type"]} for c in contrib_items[:3]]

                    topics.append({
                        "id": f"topic_{topic_idx + 1}",
                        "label": label,
                        "keywords": term_weights,
                        "top_terms": top_terms[:5],
                        "weight_pct": weight_pct,
                        "activity_counts": {
                            "searches": len(search_items),
                            "contributions": len(contrib_items),
                            "total": len(assigned_indices),
                        },
                        "sample_searches": sample_searches,
                        "sample_contributions": sample_contribs,
                    })

            except Exception:
                topics = []

        # Fallback keyword clustering if NMF didn't produce topics or documents are few
        if not topics and docs:
            all_words = clean_text(" ".join(docs)).split()
            word_counts = Counter(all_words)
            top_words = word_counts.most_common(15)

            # Group top words into 1-3 simple topics
            chunk_size = max(1, len(top_words) // 3)
            for i in range(0, min(len(top_words), 9), chunk_size):
                sub_words = [w for w, _ in top_words[i:i + chunk_size]]
                label = synthesize_topic_label(sub_words)
                term_weights = [{"term": w, "weight": round(c / len(all_words), 3)} for w, c in top_words[i:i + chunk_size]]
                topics.append({
                    "id": f"topic_{len(topics) + 1}",
                    "label": label,
                    "keywords": term_weights,
                    "top_terms": sub_words[:5],
                    "weight_pct": round(100.0 / max(1, (len(top_words) // chunk_size)), 1),
                    "activity_counts": {
                        "searches": len(query_rows),
                        "contributions": len(kg_rows),
                        "total": len(docs),
                    },
                    "sample_searches": [{"text": q.get("query", ""), "tool": q.get("tool_name", "")} for q in query_rows[:2]],
                    "sample_contributions": [{"title": k.get("title", ""), "node_type": k.get("node_type", "")} for k in kg_rows[:2]],
                })

        # Sort topics by weight_pct descending
        topics.sort(key=lambda t: t["weight_pct"], reverse=True)

        # -------------------------------------------------------------
        # Profiles, Abilities, and Personas Analysis
        # -------------------------------------------------------------
        # Extract ability tool calls & rules
        ability_tool_calls: Dict[str, int] = {}
        for tr in tool_rows:
            server = (tr.get("mcp_server") or "").lower()
            tname = (tr.get("tool_name") or "").lower()
            calls = int(tr.get("calls") or 0)
            if server == "savant-abilities" or "ability" in tname or "persona" in tname:
                ability_tool_calls[tname] = ability_tool_calls.get(tname, 0) + calls

        # Score personas: Architect vs Engineer vs Product
        corpus_text = " ".join(docs).lower()
        tool_names_text = " ".join((tr.get("tool_name") or "").lower() for tr in tool_rows)

        architect_score = 0
        engineer_score = 0
        product_score = 0

        # Lexical topic indicators
        architect_score += len(re.findall(r"\b(architect|architecture|boundary|boundaries|system|systems|plane|domain|infra|gate)\b", corpus_text)) * 2
        engineer_score += len(re.findall(r"\b(engineer|code|ast|api|rest|endpoint|query|database|postgres|auth|fix|function)\b", corpus_text)) * 2
        product_score += len(re.findall(r"\b(product|trader|user|workflow|backlog|feature|milestone|requirement|story)\b", corpus_text)) * 2

        # Tool indicators
        for tr in tool_rows:
            tname = (tr.get("tool_name") or "").lower()
            calls = int(tr.get("calls") or 0)
            if tname in ("get_lossless_tree", "neighbors", "my_domains", "commit_workspace"):
                architect_score += calls
            elif tname in ("analyze_code", "research", "search", "store", "connect"):
                engineer_score += calls
            elif tname in ("assign_session_to_workspace", "list_workspaces"):
                product_score += calls

        # Normalized persona distribution
        total_score = max(1, architect_score + engineer_score + product_score)
        arch_pct = round((architect_score / total_score) * 100, 1)
        eng_pct = round((engineer_score / total_score) * 100, 1)
        prod_pct = round((product_score / total_score) * 100, 1)

        # Dominant persona & archetype inference
        if architect_score >= engineer_score and architect_score >= product_score:
            dominant_persona = "architect"
            archetype_title = "Systems & Architecture Lead"
            archetype_desc = "Primary focus on architectural boundaries, release gates, durable knowledge modeling, and system-level invariants."
            confidence = min(0.95, round(arch_pct / 100 + 0.1, 2))
        elif engineer_score >= product_score:
            dominant_persona = "engineer"
            archetype_title = "Core Fullstack Engineer"
            archetype_desc = "Primary focus on code intelligence, AST exploration, secure API implementation, and hands-on feature construction."
            confidence = min(0.95, round(eng_pct / 100 + 0.1, 2))
        else:
            dominant_persona = "product"
            archetype_title = "Product & Workflow Strategist"
            archetype_desc = "Primary focus on delivery pipelines, workspace inventory, user workflows, and end-to-end product value."
            confidence = min(0.95, round(prod_pct / 100 + 0.1, 2))

        # Built-in abilities & rules mappings
        profiles_breakdown = [
            {"name": "architect", "type": "persona", "score": architect_score, "pct": arch_pct, "description": "Focus on explicit boundaries, predictable operations, and safe evolution."},
            {"name": "engineer", "type": "persona", "score": engineer_score, "pct": eng_pct, "description": "Focus on correctness, practical implementation, and clear explanations."},
            {"name": "product", "type": "persona", "score": product_score, "pct": prod_pct, "description": "Focus on user value, workflows, and simple defaults."},
        ]

        # Top rules inferred from user's topics and tools
        top_rules = [
            {"name": "boundaries", "type": "rule", "status": "Active", "description": "Strict isolation between subsystems and explicit interface boundaries."},
            {"name": "delivery", "type": "rule", "status": "Active", "description": "Deterministic release gates, automated smoke runs, and delivery discipline."},
            {"name": "backend_api", "type": "rule", "status": "Active", "description": "Strict schema validation, error wrapping, and stateless API contracts."},
            {"name": "frontend_ui", "type": "rule", "status": "Active", "description": "Cyberpunk terminal styling, responsive state, and resilient UI error states."},
        ]

        # -------------------------------------------------------------
        # Searches vs Contributions Comparison
        # -------------------------------------------------------------
        search_topic_counts: Dict[str, int] = {}
        contrib_topic_counts: Dict[str, int] = {}
        for t in topics:
            label = t["label"]
            search_topic_counts[label] = search_topic_counts.get(label, 0) + t["activity_counts"]["searches"]
            contrib_topic_counts[label] = contrib_topic_counts.get(label, 0) + t["activity_counts"]["contributions"]

        top_search_topics = [
            {"label": lbl, "count": count, "pct": round((count / max(1, len(query_rows))) * 100, 1)}
            for lbl, count in sorted(search_topic_counts.items(), key=lambda x: x[1], reverse=True)
            if count > 0
        ]
        top_contribution_topics = [
            {"label": lbl, "count": count, "pct": round((count / max(1, len(kg_rows))) * 100, 1)}
            for lbl, count in sorted(contrib_topic_counts.items(), key=lambda x: x[1], reverse=True)
            if count > 0
        ]

        # -------------------------------------------------------------
        # Login & Temporal Behavior Insights
        # -------------------------------------------------------------
        total_logins = sum(int(r.get("logins") or 0) for r in login_rows)
        active_login_days = len(login_rows)

        if active_login_days >= 5:
            cadence = "Consistent Daily Contributor"
        elif active_login_days >= 2:
            cadence = "Regular Weekly Operator"
        elif active_login_days == 1:
            cadence = "Periodic Deep Diver"
        else:
            cadence = "Onboarding / New Member"

        total_tool_calls = sum(int(tr.get("calls") or 0) for tr in tool_rows)

        return {
            "user_id": user_id,
            "days_analyzed": days,
            "total_documents_analyzed": len(docs),
            "archetype": {
                "title": archetype_title,
                "description": archetype_desc,
                "confidence": confidence,
                "dominant_persona": dominant_persona,
            },
            "topics": topics,
            "top_search_topics": top_search_topics,
            "top_contribution_topics": top_contribution_topics,
            "abilities_profiles": {
                "dominant_persona": dominant_persona,
                "personas": profiles_breakdown,
                "top_rules": top_rules,
                "ability_tool_calls": ability_tool_calls,
            },
            "activity_summary": {
                "total_searches": len(query_rows),
                "total_contributions": len(kg_rows),
                "total_tool_calls": total_tool_calls,
                "total_logins": total_logins,
                "active_login_days": active_login_days,
                "cadence_pattern": cadence,
                "ratio_searches_to_contributions": round(len(query_rows) / max(1, len(kg_rows)), 2),
            },
        }
