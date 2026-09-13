"""Read-only customer Build Room projection.

The Build Room deliberately owns presentation models only.  It accepts canonical
records from the platform and never writes execution state.
"""

from .projection import BuildRoomProjection, ProjectionError, ScopedEnvelope, project_build_room
from .serialization import render_data_js

__all__ = ["BuildRoomProjection", "ProjectionError", "ScopedEnvelope", "project_build_room", "render_data_js"]
