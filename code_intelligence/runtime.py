"""Production composition root for provider-neutral code intelligence."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from context.db import ContextDB
from db.code_intelligence import CodeIntelligenceConfigDB
from db.graphify import GraphifyDB

from .legacy_provider import LegacyCodeIntelligenceProvider
from .registry import CodeIntelligenceProviderRegistry
from .service import CodeIntelligenceService
from .comparison import BoundedComparisonRecorder

_shadow_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="code-intelligence-shadow")
_comparison_recorder = BoundedComparisonRecorder()


def _authorize_repo(repo_id: str, root: Path):
    record = ContextDB.get_repo_by_identifier(str(repo_id))
    if not record:
        raise PermissionError("repository not authorized")
    approved = Path(record["path"]).expanduser().resolve()
    if root.expanduser().resolve() != approved:
        raise PermissionError("repository root does not match registered root")


def build_service():
    legacy = LegacyCodeIntelligenceProvider(context_db=ContextDB, graphify_db=GraphifyDB)
    providers = {"legacy": legacy}
    try:
        from .bridge_client import CodeGraphBridgeClient
        from .codegraph_provider import CodeGraphProvider
        socket_path = os.environ.get("SAVANT_CODEGRAPH_SOCKET", "/run/savant/codegraph.sock")
        providers["codegraph"] = CodeGraphProvider(CodeGraphBridgeClient(socket_path=socket_path))
    except (ImportError, TypeError):
        # A persisted codegraph selection remains visible as unavailable rather than
        # silently changing configuration; registry lookup will fail clearly.
        pass
    registry = CodeIntelligenceProviderRegistry(
        providers,
        selection_loader=CodeIntelligenceConfigDB.provider_for_repo,
    )
    return CodeIntelligenceService(
        registry,
        authorize_repo=_authorize_repo,
        rollout_state_loader=lambda repo_id: (CodeIntelligenceConfigDB.get(repo_id) or {}).get("rollout_state", "legacy"),
        shadow_executor=_shadow_executor,
        comparison_recorder=_comparison_recorder,
    )
