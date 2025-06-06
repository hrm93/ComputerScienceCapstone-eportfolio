# buffer_processor.py

import logging
import warnings
from typing import Optional, Union
from pyproj import CRS

import geopandas as gpd
import pandas as pd
import fiona.errors

from .spatial_utils import validate_and_reproject_crs, ensure_projected_crs, buffer_intersects_gas_lines
from . import spatial_utils
from gis_tool import config
from gis_tool.utils import convert_ft_to_m, clean_geodataframe
from gis_tool.geometry_cleaning import fix_geometry
from gis_tool.parallel_utils import parallel_process
from gis_tool.buffer_utils import (
    buffer_geometry_helper,
    subtract_park_from_geom_helper,
    subtract_park_from_geom,
    log_and_filter_invalid_geometries,
)
logger = logging.getLogger("gis_tool")


def create_buffer_with_geopandas(
    input_gas_lines_path: str,
    buffer_distance_ft: Optional[float] = None,
    parks_path: Optional[str] = None,
    use_multiprocessing: bool = False,
) -> gpd.GeoDataFrame:
    """
    Create a buffer polygon around gas lines features using GeoPandas.

    Args:
        input_gas_lines_path (str): File path to input gas lines layer (shapefile, GeoPackage, etc.).
        buffer_distance_ft (float, optional): Buffer distance in feet.
        parks_path (str, optional): File path to park polygons to subtract from buffer.
        use_multiprocessing (bool, optional): Whether to use parallel processing to speed up buffering and subtraction.

    Returns:
        gpd.GeoDataFrame: A GeoDataFrame containing the buffered geometries (with parks subtracted if applicable).

    Notes:
        - Parallel processing is recommended for large datasets or complex geometries to improve performance.
        - Using multiprocessing with GeoPandas/Shapely may introduce serialization overhead or issues with very large or complex geometries.
        - For small datasets or simple features, serial processing may be faster and more stable.
        - Alternative parallel backends like Dask or GeoPandas' experimental parallel features may offer improved scalability in the future.
    """
    logger.info(f"create_buffer_with_geopandas called with input: {input_gas_lines_path}")
    if buffer_distance_ft is None:
        buffer_distance_ft = config.DEFAULT_BUFFER_DISTANCE_FT
    buffer_distance_m = convert_ft_to_m(buffer_distance_ft)
    logger.debug(f"Buffer distance in meters: {buffer_distance_m}")

    try:
        gas_lines_gdf = gpd.read_file(input_gas_lines_path)
        logger.debug("Gas lines layer loaded.")

        if gas_lines_gdf.crs is None:
            warnings.warn("Input gas lines layer has no CRS. Assigning default CRS.", UserWarning)
            logger.warning("Input gas lines layer has no CRS; assigning default.")
            gas_lines_gdf = gas_lines_gdf.set_crs(config.DEFAULT_CRS)

        gas_lines_gdf = spatial_utils.ensure_projected_crs(gas_lines_gdf)

        # === VALIDATION CHECKS BEFORE BUFFERING ===
        allowed_geom_types = ['Point', 'LineString', 'MultiLineString', 'MultiPoint']
        invalid_geom_types = gas_lines_gdf.geom_type[~gas_lines_gdf.geom_type.isin(allowed_geom_types)]
        if not invalid_geom_types.empty:
            logger.warning(
                f"Unsupported geometry types found in gas lines for buffering: {invalid_geom_types.unique()}. "
                "These features will be excluded from buffering."
            )
            gas_lines_gdf = gas_lines_gdf[gas_lines_gdf.geom_type.isin(allowed_geom_types)]

        gas_lines_gdf = log_and_filter_invalid_geometries(gas_lines_gdf, "Gas Lines")

        if gas_lines_gdf.empty:
            warnings.warn("No valid gas line geometries found for buffering after validation.", UserWarning)
            logger.warning("Gas lines GeoDataFrame is empty after filtering invalid geometries.")
            return gas_lines_gdf  # Return empty GeoDataFrame early

        # === BUFFERING ===
        if use_multiprocessing:
            logger.info("Buffering geometries with multiprocessing.")
            args = [(geom, buffer_distance_m) for geom in gas_lines_gdf.geometry]
            gas_lines_gdf['geometry'] = parallel_process(buffer_geometry_helper, args)
        else:
            logger.info("Buffering geometries sequentially.")
            gas_lines_gdf['geometry'] = gas_lines_gdf.geometry.buffer(buffer_distance_m)

        # Fix geometries after buffering
        gas_lines_gdf['geometry'] = gas_lines_gdf.geometry.apply(fix_geometry)

        # Log and filter invalid geometries after buffering
        gas_lines_gdf = log_and_filter_invalid_geometries(gas_lines_gdf, "Buffered Gas Lines")

        if gas_lines_gdf.empty:
            warnings.warn("No valid buffer geometries remain after buffering.", UserWarning)
            logger.warning("Buffered gas lines GeoDataFrame is empty after filtering invalid geometries.")
            return gas_lines_gdf  # Return early if empty

        # Subtract parks if provided
        if parks_path:
            warnings.warn("Subtracting parks from buffers. Ensure parks data is clean and valid.", UserWarning)
            logger.info(f"Subtracting parks from buffers using parks layer at {parks_path}")
            gas_lines_gdf = subtract_parks_from_buffer(gas_lines_gdf, parks_path)

        logger.debug("Buffering complete.")

        # Filter to keep only buffered polygons that intersect original gas lines
        gas_lines_gdf = gas_lines_gdf[
            gas_lines_gdf.geometry.apply(lambda buf_geom: buffer_intersects_gas_lines(buf_geom, gas_lines_gdf))
        ]

        # Clean the GeoDataFrame before final validation
        gas_lines_gdf = clean_geodataframe(gas_lines_gdf)

        # FINAL geometry validation before returning
        gas_lines_gdf = gas_lines_gdf[gas_lines_gdf.geometry.is_valid & ~gas_lines_gdf.geometry.is_empty]

        logger.info(f"Final GeoDataFrame contains {len(gas_lines_gdf)} valid, non-empty geometries.")
        return gas_lines_gdf

    except Exception as e:
        logger.exception(f"Error in create_buffer_with_geopandas: {e}")
        warnings.warn(f"Error creating buffer: {e}", UserWarning)
        raise


def subtract_parks_from_buffer(
    buffer_gdf: gpd.GeoDataFrame,
    parks_path: Optional[str] = None,
    use_multiprocessing: bool = False,
) -> gpd.GeoDataFrame:
    """
      Subtract park polygons from buffer polygons.

      Args:
          buffer_gdf: GeoDataFrame of buffered gas lines (polygons).
          parks_path: File path to park polygons layer.
          use_multiprocessing: If True, subtract parks using multiprocessing.

      Returns:
          GeoDataFrame with parks subtracted from buffer polygons.
      """
    logger.info(f"subtract_parks_from_buffer called with parks_path: {parks_path}")
    try:
        if parks_path is None:
            logger.info("No parks path provided, returning buffer unchanged.")
            return buffer_gdf.copy()

        # Load parks layer
        parks_gdf = gpd.read_file(parks_path)
        logger.debug("Parks layer loaded successfully.")

        # Validate and reproject CRS of parks_gdf using centralized helper
        parks_gdf = validate_and_reproject_crs(parks_gdf, buffer_gdf.crs, "parks")

        # === VALIDATION CHECKS FOR PARKS ===
        allowed_park_types = ['Polygon', 'MultiPolygon']
        invalid_park_types = parks_gdf.geom_type[~parks_gdf.geom_type.isin(allowed_park_types)]
        if not invalid_park_types.empty:
            logger.warning(
                f"Unsupported geometry types in parks layer: {invalid_park_types.unique()}. These will be excluded."
            )
            parks_gdf = parks_gdf[parks_gdf.geom_type.isin(allowed_park_types)]

        # Fix geometries to ensure validity
        parks_gdf = parks_gdf[parks_gdf.geometry.notnull()]
        parks_gdf['geometry'] = parks_gdf.geometry.apply(fix_geometry)
        parks_gdf = log_and_filter_invalid_geometries(parks_gdf, "Parks")

        buffer_gdf = buffer_gdf[buffer_gdf.geometry.notnull()]
        buffer_gdf['geometry'] = buffer_gdf.geometry.apply(fix_geometry)
        buffer_gdf = log_and_filter_invalid_geometries(buffer_gdf, "Buffers")

        if parks_gdf.empty:
            warnings.warn("No valid park geometries found for subtraction.", UserWarning)
            logger.warning("No valid park geometries found for subtraction.")

        parks_geoms = list(parks_gdf.geometry)
        logger.debug(f"Number of valid park geometries: {len(parks_geoms)}")

        # Subtract parks from buffers
        if use_multiprocessing:
            logger.info("Subtracting parks using multiprocessing.")
            args = [(geom, parks_geoms) for geom in buffer_gdf.geometry]
            buffer_gdf['geometry'] = parallel_process(subtract_park_from_geom_helper, args)
        else:
            logger.info("Subtracting parks sequentially.")
            buffer_gdf['geometry'] = buffer_gdf.geometry.apply(
                lambda geom: subtract_park_from_geom(geom, parks_geoms)
            )

        # Final geometry fixes and cleanup
        buffer_gdf['geometry'] = buffer_gdf.geometry.apply(fix_geometry)
        buffer_gdf = buffer_gdf[buffer_gdf.geometry.is_valid & ~buffer_gdf.geometry.is_empty]

        logger.info(f"Subtraction complete. Remaining features: {len(buffer_gdf)}")
        return buffer_gdf

    except Exception as e:
        logger.exception(f"Error in subtract_parks_from_buffer: {e}")
        warnings.warn(f"Error subtracting parks: {e}", UserWarning)
        raise

CRSLike = Union[str, CRS]

def merge_buffers_into_planning_file(
    unique_output_buffer: str,
    future_development_feature_class: str,
    point_buffer_distance: float = 10.0,
    output_crs: Optional[CRSLike] = None,
) -> gpd.GeoDataFrame:
    """
       Merge buffer polygons into a Future Development planning layer by appending features.

       ⚠️ USER-FACING WARNINGS:
       - This function overwrites the input Future Development shapefile.
       - No buffer geometries found; no update applied to Future Development layer.
       - Future Development layer is empty; merged output will contain only buffer polygons.
       - Future Development layer missing CRS; assigning default geographic CRS EPSG:4326.
       - Buffer layer missing CRS; assigning default projected CRS EPSG:32610.
       - Buffer layer CRS differs from Future Development CRS; reprojecting buffer layer.

       Args:
           unique_output_buffer (str): File path to the buffer polygons shapefile.
           future_development_feature_class (str): File path to the Future Development shapefile.
           point_buffer_distance (float): Buffer distance in meters to convert non-polygon features to polygons.
           output_crs (Optional[CRSLike]): CRS to which the merged GeoDataFrame will be projected before saving.

       Returns:
           gpd.GeoDataFrame: The merged GeoDataFrame.
       """
    logger.info(f"merge_buffers_into_planning_file called with buffer: {unique_output_buffer}")
    try:
        buffer_gdf = gpd.read_file(unique_output_buffer)
        future_dev_gdf = gpd.read_file(future_development_feature_class)

        if buffer_gdf.empty:
            warnings.warn(
                "No buffer geometries found; no update applied to Future Development layer.",
                UserWarning,
            )
            logger.warning("Buffer GeoDataFrame is empty; no geometries to merge.")
            logger.info(
                f"No update performed on '{future_development_feature_class}'; existing data remains unchanged."
            )
            # Return the original future development GeoDataFrame unchanged
            return future_dev_gdf

        if future_dev_gdf.empty:
            warnings.warn(
                "Future Development layer is empty; merged output will contain only buffer polygons.",
                          UserWarning,
            )
            logger.warning(
                "Future Development GeoDataFrame is empty; result will contain only buffer polygons."
            )

        # Assign CRS if missing, with warnings
        if not future_dev_gdf.crs or future_dev_gdf.crs.to_string() == '':
            warnings.warn(
                "Future Development layer missing CRS; assigning default geographic CRS EPSG:4326.",
                          UserWarning,
            )
            logger.warning("Future Development layer missing CRS; defaulting to EPSG:4326.")
            future_dev_gdf = future_dev_gdf.set_crs(config.GEOGRAPHIC_CRS, allow_override=True)

        if not buffer_gdf.crs or buffer_gdf.to_string() == '':
            warnings.warn(
                "Buffer layer missing CRS; assigning default projected CRS EPSG:32610.",
                UserWarning,
            )
            logger.warning("Buffer layer missing CRS; defaulting to EPSG:32610.")
            buffer_gdf = buffer_gdf.set_crs(config.BUFFER_LAYER_CRS, allow_override=True)

        # Validate and reproject buffer_gdf to match future_dev_gdf CRS
        buffer_gdf = validate_and_reproject_crs(buffer_gdf, future_dev_gdf.crs, "Buffer layer")

        # Ensure projected CRS for buffer before geometry operations
        buffer_gdf = ensure_projected_crs(buffer_gdf)

        # Also ensure future_dev_gdf is projected (optional, depending on downstream needs)
        future_dev_gdf = ensure_projected_crs(future_dev_gdf)

        if buffer_gdf.crs != future_dev_gdf.crs:
            warnings.warn(
                "Buffer layer CRS differs from Future Development CRS; reprojecting buffer layer.",
                          UserWarning,
            )
            logger.info(f"Reprojecting buffer from {buffer_gdf.crs} to {future_dev_gdf.crs}")
            buffer_gdf = buffer_gdf.to_crs(future_dev_gdf.crs)

        if not buffer_gdf.geom_type.isin(['Polygon', 'MultiPolygon']).all():
            raise ValueError(
                "Buffer shapefile must contain only polygon or multipolygon geometries."
            )

        # Only check future_dev_gdf geometry types if it's not empty
        if not future_dev_gdf.empty:
            unique_future_geom_types = future_dev_gdf.geom_type.unique()
            if len(unique_future_geom_types) != 1:
                raise ValueError(
                    f"Future Development shapefile has mixed geometry types: {unique_future_geom_types}"
                )

            future_geom_type = unique_future_geom_types[0]

            if future_geom_type not in ['Polygon', 'MultiPolygon']:
                logger.info(
                    f"Converting Future Development geometries from {future_geom_type} to polygons by buffering with {point_buffer_distance} meters."
                )
                if future_geom_type in ['Point', 'LineString']:
                    original_crs = future_dev_gdf.crs
                    projected = future_dev_gdf.to_crs(epsg=3857)
                    buffered = projected.geometry.buffer(point_buffer_distance).buffer(0)
                    future_dev_gdf['geometry'] = (
                        gpd.GeoSeries(buffered, crs=projected.crs).to_crs(original_crs)
                    )
                else:
                    raise ValueError(
                        f"Unsupported Future Development geometry type '{future_geom_type}' for conversion."
                    )

        # Clean and fix geometries
        future_dev_gdf['geometry'] = future_dev_gdf.geometry.apply(fix_geometry)
        buffer_gdf['geometry'] = buffer_gdf.geometry.apply(fix_geometry)

        # Filter invalid or empty geometries
        future_dev_gdf = future_dev_gdf[
            future_dev_gdf.geometry.notnull()
            & future_dev_gdf.geometry.is_valid
            & ~future_dev_gdf.geometry.is_empty
        ]
        buffer_gdf = buffer_gdf[
            buffer_gdf.geometry.notnull()
            & buffer_gdf.geometry.is_valid
            & ~buffer_gdf.geometry.is_empty]

        # Merge GeoDataFrames
        frames = [df for df in [future_dev_gdf, buffer_gdf] if not df.empty]
        merged_gdf = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=future_dev_gdf.crs)

        logger.info(f"Merged GeoDataFrame has {len(merged_gdf)} features after merging and cleaning.")

        # Reproject merged GeoDataFrame to output CRS (user-specified or original)
        if output_crs is None:
            output_crs = buffer_gdf.crs if not buffer_gdf.empty else future_dev_gdf.crs

        merged_gdf = merged_gdf.to_crs(output_crs)

        if merged_gdf.empty:
            logger.warning(
                f"Merged GeoDataFrame is empty; skipping writing to {future_development_feature_class}"
            )
            # Return empty GeoDataFrame with correct CRS without writing file
            return gpd.GeoDataFrame(geometry=[], crs=future_dev_gdf.crs)

        driver = config.get_driver_from_extension(future_development_feature_class)
        merged_gdf.to_file(future_development_feature_class, driver=driver)
        logger.info(f"Merged data saved to {future_development_feature_class}")

        return merged_gdf

    except (OSError, IOError, ValueError, fiona.errors.FionaError) as e:
        logger.exception(f"Error in merge_buffers_into_planning_file: {e}")
        raise
