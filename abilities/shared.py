from flask import Blueprint
from pathlib import Path
from typing import Optional
from .store import AbilityStore
from .resolver import Resolver
from server_paths import get_server_abilities_base_dir

abilities_bp = Blueprint("abilities", __name__)

_CATEGORIES = ("personas", "rules", "policies", "styles", "repos")

_store: Optional[AbilityStore] = None
_resolver: Optional[Resolver] = None


def _get_store() -> AbilityStore:
    global _store
    base_dir = Path(str(get_server_abilities_base_dir()))
    if _store is None or _store.base_path != base_dir:
        _store = AbilityStore(base_dir)
    # Reload on every request to pick up file changes
    _store.load()
    return _store


def _get_resolver() -> Resolver:
    global _resolver
    store = _get_store()
    if _resolver is None or _resolver.store is not store:
        _resolver = Resolver(store)
    return _resolver


def _clear_ability_cache() -> None:
    global _store, _resolver
    _store = None
    _resolver = None
