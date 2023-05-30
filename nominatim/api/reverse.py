# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of Nominatim. (https://nominatim.org)
#
# Copyright (C) 2023 by the Nominatim developer community.
# For a full list of authors see the git log.
"""
Implementation of reverse geocoding.
"""
from typing import Optional, List, Callable, Type, Tuple

import sqlalchemy as sa
from geoalchemy2 import WKTElement

from nominatim.typing import SaColumn, SaSelect, SaFromClause, SaLabel, SaRow
from nominatim.api.connection import SearchConnection
import nominatim.api.results as nres
from nominatim.api.logging import log
from nominatim.api.types import AnyPoint, DataLayer, ReverseDetails, GeometryFormat, Bbox

# In SQLAlchemy expression which compare with NULL need to be expressed with
# the equal sign.
# pylint: disable=singleton-comparison

RowFunc = Callable[[Optional[SaRow], Type[nres.ReverseResult]], Optional[nres.ReverseResult]]

def _select_from_placex(t: SaFromClause, wkt: Optional[str] = None) -> SaSelect:
    """ Create a select statement with the columns relevant for reverse
        results.
    """
    if wkt is None:
        distance = t.c.distance
        centroid = t.c.centroid
    else:
        distance = t.c.geometry.ST_Distance(wkt)
        centroid = sa.case(
                       (t.c.geometry.ST_GeometryType().in_(('ST_LineString',
                                                           'ST_MultiLineString')),
                        t.c.geometry.ST_ClosestPoint(wkt)),
                       else_=t.c.centroid).label('centroid')


    return sa.select(t.c.place_id, t.c.osm_type, t.c.osm_id, t.c.name,
                     t.c.class_, t.c.type,
                     t.c.address, t.c.extratags,
                     t.c.housenumber, t.c.postcode, t.c.country_code,
                     t.c.importance, t.c.wikipedia,
                     t.c.parent_place_id, t.c.rank_address, t.c.rank_search,
                     centroid,
                     distance.label('distance'),
                     t.c.geometry.ST_Expand(0).label('bbox'))


def _interpolated_housenumber(table: SaFromClause) -> SaLabel:
    return sa.cast(table.c.startnumber
                    + sa.func.round(((table.c.endnumber - table.c.startnumber) * table.c.position)
                                    / table.c.step) * table.c.step,
                   sa.Integer).label('housenumber')


def _interpolated_position(table: SaFromClause) -> SaLabel:
    fac = sa.cast(table.c.step, sa.Float) / (table.c.endnumber - table.c.startnumber)
    rounded_pos = sa.func.round(table.c.position / fac) * fac
    return sa.case(
             (table.c.endnumber == table.c.startnumber, table.c.linegeo.ST_Centroid()),
              else_=table.c.linegeo.ST_LineInterpolatePoint(rounded_pos)).label('centroid')


def _locate_interpolation(table: SaFromClause, wkt: WKTElement) -> SaLabel:
    """ Given a position, locate the closest point on the line.
    """
    return sa.case((table.c.linegeo.ST_GeometryType() == 'ST_LineString',
                    sa.func.ST_LineLocatePoint(table.c.linegeo, wkt)),
                   else_=0).label('position')


def _is_address_point(table: SaFromClause) -> SaColumn:
    return sa.and_(table.c.rank_address == 30,
                   sa.or_(table.c.housenumber != None,
                          table.c.name.has_key('housename')))

def _get_closest(*rows: Optional[SaRow]) -> Optional[SaRow]:
    return min(rows, key=lambda row: 1000 if row is None else row.distance)

class ReverseGeocoder:
    """ Class implementing the logic for looking up a place from a
        coordinate.
    """

    def __init__(self, conn: SearchConnection, params: ReverseDetails) -> None:
        self.conn = conn
        self.params = params


    @property
    def max_rank(self) -> int:
        """ Return the maximum configured rank.
        """
        return self.params.max_rank


    def has_geometries(self) -> bool:
        """ Check if any geometries are requested.
        """
        return bool(self.params.geometry_output)


    def layer_enabled(self, *layer: DataLayer) -> bool:
        """ Return true when any of the given layer types are requested.
        """
        return any(self.params.layers & l for l in layer)


    def layer_disabled(self, *layer: DataLayer) -> bool:
        """ Return true when none of the given layer types is requested.
        """
        return not any(self.params.layers & l for l in layer)


    def has_feature_layers(self) -> bool:
        """ Return true if any layer other than ADDRESS or POI is requested.
        """
        return self.layer_enabled(DataLayer.RAILWAY, DataLayer.MANMADE, DataLayer.NATURAL)

    def _add_geometry_columns(self, sql: SaSelect, col: SaColumn) -> SaSelect:
        if not self.has_geometries():
            return sql

        out = []

        if self.params.geometry_simplification > 0.0:
            col = col.ST_SimplifyPreserveTopology(self.params.geometry_simplification)

        if self.params.geometry_output & GeometryFormat.GEOJSON:
            out.append(col.ST_AsGeoJSON().label('geometry_geojson'))
        if self.params.geometry_output & GeometryFormat.TEXT:
            out.append(col.ST_AsText().label('geometry_text'))
        if self.params.geometry_output & GeometryFormat.KML:
            out.append(col.ST_AsKML().label('geometry_kml'))
        if self.params.geometry_output & GeometryFormat.SVG:
            out.append(col.ST_AsSVG().label('geometry_svg'))

        return sql.add_columns(*out)


    def _filter_by_layer(self, table: SaFromClause) -> SaColumn:
        if self.layer_enabled(DataLayer.MANMADE):
            exclude = []
            if self.layer_disabled(DataLayer.RAILWAY):
                exclude.append('railway')
            if self.layer_disabled(DataLayer.NATURAL):
                exclude.extend(('natural', 'water', 'waterway'))
            return table.c.class_.not_in(tuple(exclude))

        include = []
        if self.layer_enabled(DataLayer.RAILWAY):
            include.append('railway')
        if self.layer_enabled(DataLayer.NATURAL):
            include.extend(('natural', 'water', 'waterway'))
        return table.c.class_.in_(tuple(include))


    async def _find_closest_street_or_poi(self, wkt: WKTElement,
                                          distance: float) -> Optional[SaRow]:
        """ Look up the closest rank 26+ place in the database, which
            is closer than the given distance.
        """
        t = self.conn.t.placex

        sql = _select_from_placex(t, wkt)\
                .where(t.c.geometry.ST_DWithin(wkt, distance))\
                .where(t.c.indexed_status == 0)\
                .where(t.c.linked_place_id == None)\
                .where(sa.or_(t.c.geometry.ST_GeometryType()
                                          .not_in(('ST_Polygon', 'ST_MultiPolygon')),
                              t.c.centroid.ST_Distance(wkt) < distance))\
                .order_by('distance')\
                .limit(1)

        sql = self._add_geometry_columns(sql, t.c.geometry)

        restrict: List[SaColumn] = []

        if self.layer_enabled(DataLayer.ADDRESS):
            restrict.append(sa.and_(t.c.rank_address >= 26,
                                    t.c.rank_address <= min(29, self.max_rank)))
            if self.max_rank == 30:
                restrict.append(_is_address_point(t))
        if self.layer_enabled(DataLayer.POI) and self.max_rank == 30:
            restrict.append(sa.and_(t.c.rank_search == 30,
                                    t.c.class_.not_in(('place', 'building')),
                                    t.c.geometry.ST_GeometryType() != 'ST_LineString'))
        if self.has_feature_layers():
            restrict.append(sa.and_(t.c.rank_search.between(26, self.max_rank),
                                    t.c.rank_address == 0,
                                    self._filter_by_layer(t)))

        if not restrict:
            return None

        return (await self.conn.execute(sql.where(sa.or_(*restrict)))).one_or_none()


    async def _find_housenumber_for_street(self, parent_place_id: int,
                                           wkt: WKTElement) -> Optional[SaRow]:
        t = self.conn.t.placex

        sql = _select_from_placex(t, wkt)\
                .where(t.c.geometry.ST_DWithin(wkt, 0.001))\
                .where(t.c.parent_place_id == parent_place_id)\
                .where(_is_address_point(t))\
                .where(t.c.indexed_status == 0)\
                .where(t.c.linked_place_id == None)\
                .order_by('distance')\
                .limit(1)

        sql = self._add_geometry_columns(sql, t.c.geometry)

        return (await self.conn.execute(sql)).one_or_none()


    async def _find_interpolation_for_street(self, parent_place_id: Optional[int],
                                             wkt: WKTElement,
                                             distance: float) -> Optional[SaRow]:
        t = self.conn.t.osmline

        sql = sa.select(t,
                        t.c.linegeo.ST_Distance(wkt).label('distance'),
                        _locate_interpolation(t, wkt))\
                .where(t.c.linegeo.ST_DWithin(wkt, distance))\
                .where(t.c.startnumber != None)\
                .order_by('distance')\
                .limit(1)

        if parent_place_id is not None:
            sql = sql.where(t.c.parent_place_id == parent_place_id)

        inner = sql.subquery('ipol')

        sql = sa.select(inner.c.place_id, inner.c.osm_id,
                        inner.c.parent_place_id, inner.c.address,
                        _interpolated_housenumber(inner),
                        _interpolated_position(inner),
                        inner.c.postcode, inner.c.country_code,
                        inner.c.distance)

        if self.has_geometries():
            sub = sql.subquery('geom')
            sql = self._add_geometry_columns(sa.select(sub), sub.c.centroid)

        return (await self.conn.execute(sql)).one_or_none()


    async def _find_tiger_number_for_street(self, parent_place_id: int,
                                            parent_type: str, parent_id: int,
                                            wkt: WKTElement) -> Optional[SaRow]:
        t = self.conn.t.tiger

        inner = sa.select(t,
                          t.c.linegeo.ST_Distance(wkt).label('distance'),
                          _locate_interpolation(t, wkt))\
                  .where(t.c.linegeo.ST_DWithin(wkt, 0.001))\
                  .where(t.c.parent_place_id == parent_place_id)\
                  .order_by('distance')\
                  .limit(1)\
                  .subquery('tiger')

        sql = sa.select(inner.c.place_id,
                        inner.c.parent_place_id,
                        sa.literal(parent_type).label('osm_type'),
                        sa.literal(parent_id).label('osm_id'),
                        _interpolated_housenumber(inner),
                        _interpolated_position(inner),
                        inner.c.postcode,
                        inner.c.distance)

        if self.has_geometries():
            sub = sql.subquery('geom')
            sql = self._add_geometry_columns(sa.select(sub), sub.c.centroid)

        return (await self.conn.execute(sql)).one_or_none()


    async def lookup_street_poi(self,
                                wkt: WKTElement) -> Tuple[Optional[SaRow], RowFunc]:
        """ Find a street or POI/address for the given WKT point.
        """
        log().section('Reverse lookup on street/address level')
        distance = 0.006
        parent_place_id = None

        row = await self._find_closest_street_or_poi(wkt, distance)
        row_func: RowFunc = nres.create_from_placex_row
        log().var_dump('Result (street/building)', row)

        # If the closest result was a street, but an address was requested,
        # check for a housenumber nearby which is part of the street.
        if row is not None:
            if self.max_rank > 27 \
               and self.layer_enabled(DataLayer.ADDRESS) \
               and row.rank_address <= 27:
                distance = 0.001
                parent_place_id = row.place_id
                log().comment('Find housenumber for street')
                addr_row = await self._find_housenumber_for_street(parent_place_id, wkt)
                log().var_dump('Result (street housenumber)', addr_row)

                if addr_row is not None:
                    row = addr_row
                    row_func = nres.create_from_placex_row
                    distance = addr_row.distance
                elif row.country_code == 'us' and parent_place_id is not None:
                    log().comment('Find TIGER housenumber for street')
                    addr_row = await self._find_tiger_number_for_street(parent_place_id,
                                                                        row.osm_type,
                                                                        row.osm_id,
                                                                        wkt)
                    log().var_dump('Result (street Tiger housenumber)', addr_row)

                    if addr_row is not None:
                        row = addr_row
                        row_func = nres.create_from_tiger_row
            else:
                distance = row.distance

        # Check for an interpolation that is either closer than our result
        # or belongs to a close street found.
        if self.max_rank > 27 and self.layer_enabled(DataLayer.ADDRESS):
            log().comment('Find interpolation for street')
            addr_row = await self._find_interpolation_for_street(parent_place_id,
                                                                 wkt, distance)
            log().var_dump('Result (street interpolation)', addr_row)
            if addr_row is not None:
                row = addr_row
                row_func = nres.create_from_osmline_row

        return row, row_func


    async def _lookup_area_address(self, wkt: WKTElement) -> Optional[SaRow]:
        """ Lookup large addressable areas for the given WKT point.
        """
        log().comment('Reverse lookup by larger address area features')
        t = self.conn.t.placex

        # The inner SQL brings results in the right order, so that
        # later only a minimum of results needs to be checked with ST_Contains.
        inner = sa.select(t, sa.literal(0.0).label('distance'))\
                  .where(t.c.rank_search.between(5, self.max_rank))\
                  .where(t.c.rank_address.between(5, 25))\
                  .where(t.c.geometry.ST_GeometryType().in_(('ST_Polygon', 'ST_MultiPolygon')))\
                  .where(t.c.geometry.intersects(wkt))\
                  .where(t.c.name != None)\
                  .where(t.c.indexed_status == 0)\
                  .where(t.c.linked_place_id == None)\
                  .where(t.c.type != 'postcode')\
                  .order_by(sa.desc(t.c.rank_search))\
                  .limit(50)\
                  .subquery('area')

        sql = _select_from_placex(inner)\
                  .where(inner.c.geometry.ST_Contains(wkt))\
                  .order_by(sa.desc(inner.c.rank_search))\
                  .limit(1)

        sql = self._add_geometry_columns(sql, inner.c.geometry)

        address_row = (await self.conn.execute(sql)).one_or_none()
        log().var_dump('Result (area)', address_row)

        if address_row is not None and address_row.rank_search < self.max_rank:
            log().comment('Search for better matching place nodes inside the area')
            inner = sa.select(t,
                              t.c.geometry.ST_Distance(wkt).label('distance'))\
                      .where(t.c.osm_type == 'N')\
                      .where(t.c.rank_search > address_row.rank_search)\
                      .where(t.c.rank_search <= self.max_rank)\
                      .where(t.c.rank_address.between(5, 25))\
                      .where(t.c.name != None)\
                      .where(t.c.indexed_status == 0)\
                      .where(t.c.linked_place_id == None)\
                      .where(t.c.type != 'postcode')\
                      .where(t.c.geometry
                                .ST_Buffer(sa.func.reverse_place_diameter(t.c.rank_search))
                                .intersects(wkt))\
                      .order_by(sa.desc(t.c.rank_search))\
                      .limit(50)\
                      .subquery('places')

            touter = self.conn.t.placex.alias('outer')
            sql = _select_from_placex(inner)\
                  .join(touter, touter.c.geometry.ST_Contains(inner.c.geometry))\
                  .where(touter.c.place_id == address_row.place_id)\
                  .where(inner.c.distance < sa.func.reverse_place_diameter(inner.c.rank_search))\
                  .order_by(sa.desc(inner.c.rank_search), inner.c.distance)\
                  .limit(1)

            sql = self._add_geometry_columns(sql, inner.c.geometry)

            place_address_row = (await self.conn.execute(sql)).one_or_none()
            log().var_dump('Result (place node)', place_address_row)

            if place_address_row is not None:
                return place_address_row

        return address_row


    async def _lookup_area_others(self, wkt: WKTElement) -> Optional[SaRow]:
        t = self.conn.t.placex

        inner = sa.select(t, t.c.geometry.ST_Distance(wkt).label('distance'))\
                  .where(t.c.rank_address == 0)\
                  .where(t.c.rank_search.between(5, self.max_rank))\
                  .where(t.c.name != None)\
                  .where(t.c.indexed_status == 0)\
                  .where(t.c.linked_place_id == None)\
                  .where(self._filter_by_layer(t))\
                  .where(t.c.geometry
                                .ST_Buffer(sa.func.reverse_place_diameter(t.c.rank_search))
                                .intersects(wkt))\
                  .order_by(sa.desc(t.c.rank_search))\
                  .limit(50)\
                  .subquery()

        sql = _select_from_placex(inner)\
                  .where(sa.or_(inner.c.geometry.ST_GeometryType()
                                                .not_in(('ST_Polygon', 'ST_MultiPolygon')),
                                inner.c.geometry.ST_Contains(wkt)))\
                  .order_by(sa.desc(inner.c.rank_search), inner.c.distance)\
                  .limit(1)

        sql = self._add_geometry_columns(sql, inner.c.geometry)

        row = (await self.conn.execute(sql)).one_or_none()
        log().var_dump('Result (non-address feature)', row)

        return row


    async def lookup_area(self, wkt: WKTElement) -> Optional[SaRow]:
        """ Lookup large areas for the given WKT point.
        """
        log().section('Reverse lookup by larger area features')

        if self.layer_enabled(DataLayer.ADDRESS):
            address_row = await self._lookup_area_address(wkt)
        else:
            address_row = None

        if self.has_feature_layers():
            other_row = await self._lookup_area_others(wkt)
        else:
            other_row = None

        return _get_closest(address_row, other_row)


    async def lookup_country(self, wkt: WKTElement) -> Optional[SaRow]:
        """ Lookup the country for the given WKT point.
        """
        log().section('Reverse lookup by country code')
        t = self.conn.t.country_grid
        sql = sa.select(t.c.country_code).distinct()\
                .where(t.c.geometry.ST_Contains(wkt))

        ccodes = tuple((r[0] for r in await self.conn.execute(sql)))
        log().var_dump('Country codes', ccodes)

        if not ccodes:
            return None

        t = self.conn.t.placex
        if self.max_rank > 4:
            log().comment('Search for place nodes in country')

            inner = sa.select(t,
                              t.c.geometry.ST_Distance(wkt).label('distance'))\
                      .where(t.c.osm_type == 'N')\
                      .where(t.c.rank_search > 4)\
                      .where(t.c.rank_search <= self.max_rank)\
                      .where(t.c.rank_address.between(5, 25))\
                      .where(t.c.name != None)\
                      .where(t.c.indexed_status == 0)\
                      .where(t.c.linked_place_id == None)\
                      .where(t.c.type != 'postcode')\
                      .where(t.c.country_code.in_(ccodes))\
                      .where(t.c.geometry
                                .ST_Buffer(sa.func.reverse_place_diameter(t.c.rank_search))
                                .intersects(wkt))\
                      .order_by(sa.desc(t.c.rank_search))\
                      .limit(50)\
                      .subquery()

            sql = _select_from_placex(inner)\
                  .where(inner.c.distance < sa.func.reverse_place_diameter(inner.c.rank_search))\
                  .order_by(sa.desc(inner.c.rank_search), inner.c.distance)\
                  .limit(1)

            sql = self._add_geometry_columns(sql, inner.c.geometry)

            address_row = (await self.conn.execute(sql)).one_or_none()
            log().var_dump('Result (addressable place node)', address_row)
        else:
            address_row = None

        if address_row is None:
            # Still nothing, then return a country with the appropriate country code.
            sql = _select_from_placex(t, wkt)\
                      .where(t.c.country_code.in_(ccodes))\
                      .where(t.c.rank_address == 4)\
                      .where(t.c.rank_search == 4)\
                      .where(t.c.linked_place_id == None)\
                      .order_by('distance')\
                      .limit(1)

            sql = self._add_geometry_columns(sql, t.c.geometry)

            address_row = (await self.conn.execute(sql)).one_or_none()

        return address_row


    async def lookup(self, coord: AnyPoint) -> Optional[nres.ReverseResult]:
        """ Look up a single coordinate. Returns the place information,
            if a place was found near the coordinates or None otherwise.
        """
        log().function('reverse_lookup', coord=coord, params=self.params)


        wkt = WKTElement(f'POINT({coord[0]} {coord[1]})', srid=4326)

        row: Optional[SaRow] = None
        row_func: RowFunc = nres.create_from_placex_row

        if self.max_rank >= 26:
            row, tmp_row_func = await self.lookup_street_poi(wkt)
            if row is not None:
                row_func = tmp_row_func
        if row is None and self.max_rank > 4:
            row = await self.lookup_area(wkt)
        if row is None and self.layer_enabled(DataLayer.ADDRESS):
            row = await self.lookup_country(wkt)

        result = row_func(row, nres.ReverseResult)
        if result is not None:
            assert row is not None
            result.distance = row.distance
            if hasattr(row, 'bbox'):
                result.bbox = Bbox.from_wkb(row.bbox.data)
            await nres.add_result_details(self.conn, [result], self.params)

        return result
