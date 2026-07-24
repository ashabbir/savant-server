"""
Flask Blueprint for Abilities REST API.

All routes under /api/abilities/*.
The MCP server and (future) UI both call these endpoints.
"""

# Import blueprint from the shared file
from .shared import abilities_bp

# Import sub-route modules to register their endpoints on the blueprint
from . import assets_routes, matching_routes, admin_routes
