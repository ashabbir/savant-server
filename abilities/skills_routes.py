"""
Flask Blueprint for Skills REST API.
"""

# Import blueprint and directory from the shared file for backward compatibility
from .skills_shared import skills_bp, SKILLS_DIR

# Import sub-route modules to register their endpoints on the blueprint
from . import skills_file_routes, skills_admin_routes
