"""Data sources, licences and the non-affiliation notice, in one place.

Wording follows Overture's attribution page (docs.overturemaps.org/attribution):
the transportation and divisions themes are licensed under the Open Database
License and require "© OpenStreetMap contributors". Overture's own recommended
citation is "Overture Maps Foundation, overturemaps.org".
"""

from __future__ import annotations

OVERTURE_CITATION = "Overture Maps Foundation, overturemaps.org"

DATA_ATTRIBUTION = (
    "Road and boundary data © OpenStreetMap contributors, Overture Maps Foundation. "
    "Available under the Open Database License (ODbL)."
)

SOURCE_DETAIL = (
    "Overture's transportation theme includes contributions from TomTom and OpenStreetMap. "
    "Its divisions theme also draws on geoBoundaries, Esri Community Maps and LINZ (CC BY 4.0)."
)

PLACES_ATTRIBUTION = (
    "Places data from Overture Maps Foundation, combining sources under CDLA Permissive 2.0 "
    "(Meta, Microsoft and others), Apache 2.0 (Foursquare Labs, Inc.) and CC0 1.0 (AllThePlaces)."
)

BASEMAP_ATTRIBUTION = "Basemap © CARTO, © OpenStreetMap contributors."

NOT_AFFILIATED = (
    "Independent project. Not affiliated with, endorsed by or sponsored by the "
    "Overture Maps Foundation, the OpenStreetMap Foundation or TomTom."
)

SAMPLE_DATA_NOTE = (
    "The Berlin charging sessions and districts sample is synthetic. Operator names are "
    "illustrative and the sessions do not describe any real operator."
)

LICENSE = "ODbL-1.0"
LICENSE_URL = "https://opendatacommons.org/licenses/odbl/"


def scope_fields() -> dict:
    """Licence fields recorded next to every fetched release."""
    return {
        "source": OVERTURE_CITATION,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "attribution": DATA_ATTRIBUTION,
    }
