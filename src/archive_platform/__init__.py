"""Deployment-oriented archive platform adapters.

The existing JSON/JSONL files remain the migration source of truth.  This
package adds a bounded Neon read model and API contract without changing the
legacy builders or approval promotion flow.
"""

SCHEMA_NAME = "image_archive"
