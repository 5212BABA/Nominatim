# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of Nominatim. (https://nominatim.org)
#
# Copyright (C) 2023 by the Nominatim developer community.
# For a full list of authors see the git log.
"""
The public interface of the Nominatim library.

Classes and functions defined in this file are considered stable. Always
import from this file, not from the source files directly.
"""

# See also https://github.com/PyCQA/pylint/issues/6006
# pylint: disable=useless-import-alias

from .core import (NominatimAPI as NominatimAPI,
                   NominatimAPIAsync as NominatimAPIAsync)
from .status import (StatusResult as StatusResult)
from .types import (PlaceID as PlaceID,
                    OsmID as OsmID,
                    PlaceRef as PlaceRef,
                    Point as Point,
                    Bbox as Bbox,
                    GeometryFormat as GeometryFormat,
                    LookupDetails as LookupDetails,
                    DataLayer as DataLayer)
from .results import (SourceTable as SourceTable,
                      AddressLine as AddressLine,
                      AddressLines as AddressLines,
                      WordInfo as WordInfo,
                      WordInfos as WordInfos,
                      DetailedResult as DetailedResult,
                      ReverseResult as ReverseResult,
                      ReverseResults as ReverseResults,
                      SearchResult as SearchResult,
                      SearchResults as SearchResults)
from .localization import (Locales as Locales)
