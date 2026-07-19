"""Production composition root for provider-neutral code intelligence."""

import os
from pathlib import Path

from context.db import ContextDB
from db.code_intelligence import CodeIntelligenceConfigDB
from .bridge_client import CodeGraphBridgeClient
from .codegraph_provider import CodeGraphProvider
from .registry import CodeIntelligenceProviderRegistry
from .service import CodeIntelligenceService


def _authorize_repo(repo_id: str, root: Path):
    record = ContextDB.get_repo_by_identifier(str(repo_id))
    if not record:
        raise PermissionError("repository not authorized")
    approved = Path(record["path"]).expanduser().resolve()
    if root.expanduser().resolve() != approved:
        raise PermissionError("repository root does not match registered root")


def build_service():
    socket_path = os.environ.get("SAVANT_CODEGRAPH_SOCKET", "/run/savant/codegraph.sock")
    providers = {"codegraph": CodeGraphProvider(CodeGraphBridgeClient(socket_path=socket_path))}
    registry = CodeIntelligenceProviderRegistry(
        providers,
        selection_loader=CodeIntelligenceConfigDB.provider_for_repo,
    )
    return CodeIntelligenceService(
        registry,
        authorize_repo=_authorize_repo,
    )
