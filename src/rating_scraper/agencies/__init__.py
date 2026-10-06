"""Rating agency adapters."""
from .acuite import AcuiteAdapter
from .base import AgencyAdapter
from .brickwork import BrickworkAdapter
from .care import CareAdapter
from .crisil import CrisilAdapter
from .icra import IcraAdapter
from .indiaratings import IndiaRatingsAdapter
from .infomerics import InfomericsAdapter

# Agencies are registered here; the pipeline iterates this mapping.
ADAPTERS = {
    "CARE": CareAdapter,
    "CRISIL": CrisilAdapter,
    "ICRA": IcraAdapter,
    "IndiaRatings": IndiaRatingsAdapter,
    "Brickwork": BrickworkAdapter,
    "Infomerics": InfomericsAdapter,
    "Acuite": AcuiteAdapter,
}

__all__ = [
    "AcuiteAdapter",
    "AgencyAdapter",
    "BrickworkAdapter",
    "CareAdapter",
    "CrisilAdapter",
    "IcraAdapter",
    "IndiaRatingsAdapter",
    "InfomericsAdapter",
    "ADAPTERS",
]
