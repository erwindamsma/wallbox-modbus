"""Energy data sources.

`source.type` in the config picks one of these by name. Import a new module
here to register it.
"""

from .base import (  # noqa: F401
    EnergySource,
    SourceConfig,
    available,
    create,
    get,
    register,
)
from . import homeassistant  # noqa: F401,E402  -- imported for its side effect: registration
