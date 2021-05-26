from osgeo.osr import SpatialReference


from kart import crs_util
from kart.schema import Schema, ColumnSchema
from kart.sqlalchemy.postgis import Db_Postgis
from kart.sqlalchemy.adapter.base import BaseKartAdapter


class KartAdapter_Postgis(BaseKartAdapter, Db_Postgis):
    """
    Adapts a table in PostGIS (and the attached CRS, if there is one) to a V2 dataset.
    Or, does the reverse - adapts a V2 dataset to a PostGIS table (plus attached CRS).
    """

    V2_TYPE_TO_SQL_TYPE = {
        "boolean": "BOOLEAN",
        "blob": "BYTEA",
        "date": "DATE",
        "float": {0: "REAL", 32: "REAL", 64: "DOUBLE PRECISION"},
        "geometry": "GEOMETRY",
        "integer": {
            0: "INTEGER",
            8: "SMALLINT",  # Approximated as smallint (int16)
            16: "SMALLINT",
            32: "INTEGER",
            64: "BIGINT",
        },
        "interval": "INTERVAL",
        "numeric": "NUMERIC",
        "text": "TEXT",
        "time": "TIME",
        "timestamp": "TIMESTAMPTZ",
        # TODO - time and timestamp come in two flavours, with and without timezones.
        # Code for preserving these flavours in datasets and working copies needs more work.
    }

    SQL_TYPE_TO_V2_TYPE = {
        "BOOLEAN": "boolean",
        "SMALLINT": ("integer", 16),
        "INTEGER": ("integer", 32),
        "BIGINT": ("integer", 64),
        "REAL": ("float", 32),
        "DOUBLE PRECISION": ("float", 64),
        "BYTEA": "blob",
        "CHARACTER VARYING": "text",
        "DATE": "date",
        "GEOMETRY": "geometry",
        "INTERVAL": "interval",
        "NUMERIC": "numeric",
        "TEXT": "text",
        "TIME": "time",
        "TIMETZ": "time",
        "TIMESTAMP": "timestamp",
        "TIMESTAMPTZ": "timestamp",
        "VARCHAR": "text",
    }

    # Types that can't be roundtripped perfectly in PostGIS, and what they end up as.
    APPROXIMATED_TYPES = {("integer", 8): ("integer", 16)}

    @classmethod
    def v2_schema_to_sql_spec(cls, schema, v2_obj):
        """
        Generate the SQL CREATE TABLE spec from a V2 object eg:
        'fid INTEGER, geom GEOMETRY(POINT,2136), desc VARCHAR(128), PRIMARY KEY(fid)'
        """
        result = [
            f"{cls.quote(col.name)} {cls.v2_type_to_pg_type(col, v2_obj)}"
            for col in schema
        ]

        if schema.pk_columns:
            pk_col_names = ", ".join((cls.quote(col.name) for col in schema.pk_columns))
            result.append(f"PRIMARY KEY({pk_col_names})")

        return ", ".join(result)

    @classmethod
    def all_v2_meta_items(cls, sess, db_schema, table_name, id_salt):
        """
        Generate all V2 meta items for the given table.
        Varying the id_salt varies the ids that are generated for the schema.json item.
        """
        title = sess.scalar(
            "SELECT obj_description((:table_identifier)::regclass, 'pg_class');",
            {"table_identifier": f"{db_schema}.{table_name}"},
        )
        yield "title", title

        table_info_sql = """
            SELECT
                C.column_name, C.ordinal_position, C.data_type, C.udt_name,
                C.character_maximum_length, C.numeric_precision, C.numeric_scale,
                KCU.ordinal_position AS pk_ordinal_position,
                upper(postgis_typmod_type(A.atttypmod)) AS geometry_type,
                postgis_typmod_srid(A.atttypmod) AS geometry_srid
            FROM information_schema.columns C
            LEFT OUTER JOIN information_schema.key_column_usage KCU
            ON (KCU.table_schema = C.table_schema)
            AND (KCU.table_name = C.table_name)
            AND (KCU.column_name = C.column_name)
            LEFT OUTER JOIN pg_attribute A
            ON (A.attname = C.column_name)
            AND (A.attrelid = (C.table_schema || '.' || C.table_name)::regclass::oid)
            WHERE C.table_schema=:table_schema AND C.table_name=:table_name
            ORDER BY C.ordinal_position;
        """
        r = sess.execute(
            table_info_sql,
            {"table_schema": db_schema, "table_name": table_name},
        )
        pg_table_info = list(r)

        spatial_ref_sys_sql = """
            SELECT SRS.* FROM spatial_ref_sys SRS
            LEFT OUTER JOIN geometry_columns GC ON (GC.srid = SRS.srid)
            WHERE GC.f_table_schema=:table_schema AND GC.f_table_name=:table_name;
        """
        r = sess.execute(
            spatial_ref_sys_sql,
            {"table_schema": db_schema, "table_name": table_name},
        )
        pg_spatial_ref_sys = list(r)

        schema = cls.postgis_to_v2_schema(pg_table_info, pg_spatial_ref_sys, id_salt)
        yield "schema.json", schema.to_column_dicts()

        for crs_info in pg_spatial_ref_sys:
            wkt = crs_info["srtext"]
            id_str = crs_util.get_identifier_str(wkt)
            yield f"crs/{id_str}.wkt", crs_util.normalise_wkt(wkt)

    @classmethod
    def v2_type_to_pg_type(cls, column_schema, v2_obj):
        """Convert a v2 schema type to a postgis type."""

        v2_type = column_schema.data_type
        extra_type_info = column_schema.extra_type_info

        pg_type_info = cls.V2_TYPE_TO_SQL_TYPE.get(v2_type)
        if pg_type_info is None:
            raise ValueError(f"Unrecognised data type: {v2_type}")

        if isinstance(pg_type_info, dict):
            return pg_type_info.get(extra_type_info.get("size", 0))

        pg_type = pg_type_info
        if pg_type == "GEOMETRY":
            geometry_type = extra_type_info.get("geometryType")
            crs_name = extra_type_info.get("geometryCRS")
            crs_id = None
            if crs_name is not None:
                crs_id = crs_util.get_identifier_int_from_dataset(v2_obj, crs_name)
            return cls._v2_geometry_type_to_pg_type(geometry_type, crs_id)

        if pg_type == "TEXT":
            length = extra_type_info.get("length", None)
            return f"VARCHAR({length})" if length is not None else "TEXT"

        if pg_type == "NUMERIC":
            precision = extra_type_info.get("precision", None)
            scale = extra_type_info.get("scale", None)
            if precision is not None and scale is not None:
                return f"NUMERIC({precision},{scale})"
            elif precision is not None:
                return f"NUMERIC({precision})"
            else:
                return "NUMERIC"

        return pg_type

    @classmethod
    def _v2_geometry_type_to_pg_type(cls, geometry_type, crs_id):
        if geometry_type is not None:
            geometry_type = geometry_type.replace(" ", "")

        if geometry_type is not None and crs_id is not None:
            return f"GEOMETRY({geometry_type},{crs_id})"
        elif geometry_type is not None:
            return f"GEOMETRY({geometry_type})"
        else:
            return "GEOMETRY"

    @classmethod
    def postgis_to_v2_schema(cls, pg_table_info, pg_spatial_ref_sys, id_salt):
        """Generate a V2 schema from the given postgis metadata tables."""
        return Schema(
            [
                cls._postgis_to_column_schema(col, pg_spatial_ref_sys, id_salt)
                for col in pg_table_info
            ]
        )

    @classmethod
    def _postgis_to_column_schema(cls, pg_col_info, pg_spatial_ref_sys, id_salt):
        """
        Given the postgis column info for a particular column, and some extra context in
        case it is a geometry column, converts it to a ColumnSchema. The extra context will
        only be used if the given pg_col_info is the geometry column.
        Parameters:
        pg_col_info - info about a single column from pg_table_info.
        pg_spatial_ref_sys - rows of the "spatial_ref_sys" table that are referenced by this dataset.
        id_salt - the UUIDs of the generated ColumnSchema are deterministic and depend on
        the name and type of the column, and on this salt.
        """
        name = pg_col_info["column_name"]
        pk_index = pg_col_info["pk_ordinal_position"]
        if pk_index is not None:
            pk_index -= 1
        data_type, extra_type_info = cls._pg_type_to_v2_type(
            pg_col_info, pg_spatial_ref_sys
        )

        col_id = ColumnSchema.deterministic_id(name, data_type, id_salt)
        return ColumnSchema(col_id, name, data_type, pk_index, **extra_type_info)

    @classmethod
    def _pg_type_to_v2_type(cls, pg_col_info, pg_spatial_ref_sys):
        pg_type = pg_col_info["data_type"].upper()
        v2_type_info = cls.SQL_TYPE_TO_V2_TYPE.get(pg_type)
        if v2_type_info is None:
            v2_type_info = cls.SQL_TYPE_TO_V2_TYPE.get(pg_col_info["udt_name"].upper())

        if isinstance(v2_type_info, tuple):
            v2_type = v2_type_info[0]
            extra_type_info = {"size": v2_type_info[1]}
        else:
            v2_type = v2_type_info
            extra_type_info = {}

        if v2_type == "geometry":
            return cls._pg_type_to_v2_geometry_type(pg_col_info, pg_spatial_ref_sys)

        if v2_type == "text":
            length = pg_col_info["character_maximum_length"] or None
            if length is not None:
                extra_type_info["length"] = length

        if v2_type == "numeric":
            extra_type_info["precision"] = pg_col_info["numeric_precision"] or None
            extra_type_info["scale"] = pg_col_info["numeric_scale"] or None

        return v2_type, extra_type_info

    @classmethod
    def _pg_type_to_v2_geometry_type(cls, pg_col_info, pg_spatial_ref_sys):
        """
        col_name - the name of the column.
        pg_spatial_ref_sys - rows of the "spatial_ref_sys" table that are referenced by this dataset.
        """
        geometry_type = pg_col_info["geometry_type"].upper()
        # Look for Z, M, or ZM suffix
        geometry_type, m = cls._pop_suffix(geometry_type, "M")
        geometry_type, z = cls._pop_suffix(geometry_type, "Z")
        geometry_type = f"{geometry_type} {z}{m}".strip()

        geometry_crs = None
        crs_id = pg_col_info["geometry_srid"]
        if crs_id:
            crs_info = next(
                (r for r in pg_spatial_ref_sys if r["srid"] == crs_id), None
            )
            if crs_info:
                geometry_crs = crs_util.get_identifier_str(crs_info["srtext"])

        return "geometry", {"geometryType": geometry_type, "geometryCRS": geometry_crs}

    @classmethod
    def _pop_suffix(cls, geometry_type, suffix):
        """
        Returns (geometry-type-without-suffix, suffix) if geometry-type ends with suffix.
        Otherwise just returns (geometry-type, "")
        """
        if geometry_type.endswith(suffix):
            return geometry_type[:-1], suffix
        else:
            return geometry_type, ""

    @classmethod
    def generate_postgis_spatial_ref_sys(cls, v2_obj):
        """
        Generates the contents of the spatial_ref_sys table from the v2 object.
        The result is a list containing a dict per table row.
        Each dict has the format {column-name: value}.
        """
        result = []
        for crs_name, definition in v2_obj.crs_definitions():
            spatial_ref = SpatialReference(definition)
            auth_name = spatial_ref.GetAuthorityName(None) or "NONE"
            crs_id = crs_util.get_identifier_int(spatial_ref)
            result.append(
                {
                    "srid": crs_id,
                    "auth_name": auth_name,
                    "auth_srid": crs_id,
                    "srtext": definition,
                    "proj4text": spatial_ref.ExportToProj4(),
                }
            )
        return result

    @classmethod
    def _dimension_count(cls, geometry_type):
        # Look for Z, M, or ZM suffix
        geometry_type, m = cls._pop_suffix(geometry_type, "M")
        geometry_type, z = cls._pop_suffix(geometry_type, "Z")
        return len(f"XY{z}{m}")