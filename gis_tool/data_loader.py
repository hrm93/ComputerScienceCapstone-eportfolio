### data_loader.py
"""
data_loader.py

Module responsible for loading and processing pipeline report data from text and GeoJSON files,
creating new geospatial features for gas line infrastructure, and optionally storing/updating
these features in a MongoDB database.

Features:
- Connects to MongoDB with connection validation.
- Identifies new pipeline report files (.txt and .geojson) from a specified directory.
- Parses and validates report data, converting it into GeoDataFrames.
- Handles spatial reference systems and geometry types consistently.
- Inserts or updates pipeline features into MongoDB, avoiding duplicates.
- Persists new or updated features to an ESRI shapefile.

This module is designed for integration with a GIS pipeline processing tool,
facilitating data ingestion and feature management within a geospatial data workflow.

Dependencies:
- geopandas
- shapely
- pymongo
- pandas
- standard Python libraries: os, logging

Typical usage:
    from gis_tool.data_loader import connect_to_mongodb, find_new_reports, create_pipeline_features

    db = connect_to_mongodb()
    new_reports = find_new_reports("/path/to/reports")
    create_pipeline_features(new_reports, "gas_lines.shp", "/path/to/reports", "EPSG:4326",
                             gas_lines_collection=db['gas_lines'])
"""
import logging
from typing import Any, Union, List, Tuple, Optional, Set

import geopandas as gpd
import pandas as pd
from dateutil.parser import parse
from pymongo.collection import Collection
from shapely.geometry import Point

from gis_tool.db_utils import upsert_mongodb_feature

logger = logging.getLogger("gis_tool")

# Note: 'material' field is normalized to lowercase for consistency.
# Other string fields like 'name' retain original casing.
SCHEMA_FIELDS = ["Name", "Date", "PSI", "Material", "geometry"]


def robust_date_parse(date_val: Any) -> Union[pd.Timestamp, pd.NaT]:
    """
    Robustly parse various date formats or objects into a pandas Timestamp.

    Args:
        date_val (Any): Input date value (can be string, Timestamp, or NaN).

    Returns:
        Union[pd.Timestamp, pd.NaT]: A valid pandas Timestamp or pd.NaT if parsing fails.
    """
    logger.debug(f"Parsing date: {date_val}")
    if pd.isna(date_val):
        logger.debug("Date value is NaN or None; returning pd.NaT.")
        return pd.NaT
    if isinstance(date_val, pd.Timestamp):
        logger.debug("Date value is already a pandas Timestamp.")
        return date_val
    if isinstance(date_val, str):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                parsed = pd.to_datetime(date_val, format=fmt)
                logger.debug(f"Date parsed using format {fmt}: {parsed}")
                return parsed
            except (ValueError, TypeError):
                continue
        try:
            parsed = pd.to_datetime(parse(date_val, fuzzy=False))
            logger.debug(f"Date parsed using dateutil: {parsed}")
            return parsed
        except (ValueError, TypeError):
            logger.warning(f"Failed to parse date: {date_val}; returning pd.NaT.")
            return pd.NaT
    logger.warning(f"Unsupported date type: {type(date_val)}; returning pd.NaT.")
    return pd.NaT


def make_feature(
    name: str,
    date: Union[str, pd.Timestamp],
    psi: float,
    material: str,
    geometry: Point,
    crs: str
) -> gpd.GeoDataFrame:
    """
      Create a GeoDataFrame containing a single pipeline feature with specified attributes.

      The feature fields correspond to the predefined SCHEMA_FIELDS.
      The material string is normalized to lowercase.

      Args:
          name (str): The name/ID of the pipeline feature.
          date (Union[str, pd.Timestamp]): The date associated with the feature.
          psi (float): The pressure measurement for the pipeline.
          material (str): The material of the pipeline (case-insensitive).
          geometry (Point): The geometric location as a Shapely Point.
          crs (str): The coordinate reference system string (e.g., "EPSG:4326").

      Returns:
          gpd.GeoDataFrame: A GeoDataFrame with one row representing the feature,
          using the provided CRS.
      """
    logger.debug(
        f"Creating feature: name={name}, date={date}, psi={psi}, material={material}, "
        f"geometry={geometry.wkt}, crs={crs}"
    )
    data = {
        SCHEMA_FIELDS[0]: [name],
        SCHEMA_FIELDS[1]: [date],
        SCHEMA_FIELDS[2]: [psi],
        SCHEMA_FIELDS[3]: [material.lower()],
        SCHEMA_FIELDS[4]: [geometry]
    }
    return gpd.GeoDataFrame(data, crs=crs)


def create_pipeline_features(
    geojson_reports: List[Tuple[str, gpd.GeoDataFrame]],
    txt_reports: List[Tuple[str, List[str]]],
    gas_lines_gdf: gpd.GeoDataFrame,
    spatial_reference: str,
    gas_lines_collection: Optional[Collection] = None,
    processed_reports: Optional[Set[str]] = None,
    use_mongodb: bool = True
) -> Tuple[Set[str], gpd.GeoDataFrame, bool]:
    """
    Process GeoJSON and TXT pipeline reports to create or update gas line features.

    This function:
    - Normalizes CRS of input GeoDataFrames to the target spatial reference.
    - Parses and validates pipeline features from GeoJSON and TXT report data.
    - Adds new features or updates existing ones in the provided gas lines GeoDataFrame.
    - Optionally inserts or updates features in a MongoDB collection.
    - Tracks which reports have been processed to avoid duplicates.

    Args:
        geojson_reports (List[Tuple[str, gpd.GeoDataFrame]]):
            List of tuples containing report filename and GeoDataFrame loaded from GeoJSON files.
        txt_reports (List[Tuple[str, List[str]]]):
            List of tuples containing report filename and lines from TXT report files.
        gas_lines_gdf (gpd.GeoDataFrame):
            Existing GeoDataFrame of gas line features to update or append new features.
        spatial_reference (str):
            Target coordinate reference system (CRS) string (e.g., "EPSG:4326") to unify geometries.
        gas_lines_collection (Optional[Collection], optional):
            MongoDB collection for inserting/updating features. Defaults to None.
        processed_reports (Optional[Set[str]], optional):
            Set of report filenames already processed. Defaults to None, which initializes to empty set.
        use_mongodb (bool, optional):
            Flag to enable MongoDB upsert operations. Defaults to True.

    Returns:
        Tuple[Set[str], gpd.GeoDataFrame, bool]:
            - Updated set of processed report filenames.
            - Updated GeoDataFrame with new or updated gas line features.
            - Boolean flag indicating whether any new features were added.

    Notes:
        - Reports that are missing required fields or malformed lines are logged and skipped.
        - Geometry is simplified and CRS is normalized for consistency.
        - Duplicate features (based on name and geometry) are avoided in MongoDB.
        - Material field normalized to lowercase for consistency.
    """
    logger.info("Starting pipeline feature creation...")
    if processed_reports is None:
        processed_reports = set()

    processed_pipelines = set()
    features_added = False

    # Normalize gas_lines_gdf CRS
    if gas_lines_gdf.crs and gas_lines_gdf.crs.to_string() != spatial_reference:
        logger.info(
            f"Reprojecting gas_lines_gdf from {gas_lines_gdf.crs.to_string()} to {spatial_reference}."
        )
        gas_lines_gdf = gas_lines_gdf.to_crs(spatial_reference)

    # Normalize GeoJSON report CRS
    for i, (report_name, gdf) in enumerate(geojson_reports):
        if gdf.crs and gdf.crs.to_string() != spatial_reference:
            logger.info(
                f"Reprojecting GeoJSON report '{report_name}' from {gdf.crs.to_string()} to {spatial_reference}."
            )
            gdf = gdf.to_crs(spatial_reference)
            geojson_reports[i] = (report_name, gdf)

    def align_feature_dtypes(new_feat: gpd.GeoDataFrame, base_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        new_feat = new_feat.reindex(columns=base_gdf.columns)
        for col in base_gdf.columns:
            if col in new_feat.columns:
                try:
                    new_feat[col] = new_feat[col].astype(base_gdf[col].dtype)
                except Exception as exc:
                    logger.debug(f"Could not convert column '{col}' dtype: {exc}")
        if 'geometry' in new_feat.columns:
            new_feat.set_geometry('geometry', inplace=True)
        return new_feat

    # Process GeoJSON reports
    for report_name, gdf in geojson_reports:
        if report_name in processed_reports:
            logger.info(f"Skipping already processed report: {report_name}")
            continue

        logger.info(f"Processing GeoJSON report: {report_name}")
        required_fields = set(SCHEMA_FIELDS) - {"geometry"}
        missing_fields = required_fields - set(gdf.columns)
        if missing_fields:
            logger.error(f"GeoJSON report '{report_name}' missing required fields: {missing_fields}")
            processed_reports.add(report_name)
            continue

        for _, row in gdf.iterrows():
            point = row.geometry
            parsed_date = robust_date_parse(row["Date"])
            new_feature = make_feature(row["Name"], parsed_date, row["PSI"], row["Material"], point, spatial_reference)

            if use_mongodb and gas_lines_collection is not None:
                logger.debug(f"Inserting/updating feature in MongoDB: {row['Name']}")
                upsert_mongodb_feature(
                    gas_lines_collection,
                    row["Name"],
                    row["Date"],
                    row["PSI"],
                    row["Material"],
                    point,
                )

            new_feature = align_feature_dtypes(new_feature, gas_lines_gdf)
            valid_rows = new_feature.dropna(how="all")

            if not valid_rows.empty:
                logger.debug(f"Adding new feature: {row['Name']}")
                gas_lines_gdf = pd.concat([gas_lines_gdf, valid_rows], ignore_index=True)
                processed_pipelines.add(row["Name"])
                features_added = True

        processed_reports.add(report_name)

    # Process TXT reports
    for report_name, lines in txt_reports:
        if report_name in processed_reports:
            logger.info(f"Skipping already processed TXT report: {report_name}")
            continue

        logger.info(f"Processing TXT report: {report_name}")
        for line_number, line in enumerate(lines, start=1):
            if "Id Name" in line:
                continue

            data = line.strip().split()
            if len(data) < 7:
                logger.warning(
                    f"Skipping malformed line {line_number} in {report_name} "
                    f"(expected at least 7 fields): {line.strip()}"
                )
                continue

            try:
                line_name = data[1]
                x_coord = float(data[2])
                y_coord = float(data[3])
                date_completed = data[4]
                psi = float(data[5])
                material = data[6].lower()
            except (ValueError, IndexError) as e:
                logger.warning(
                    f"Skipping line {line_number} in {report_name} due to parse error: "
                    f"{line.strip()} | Error: {e}"
                )
                continue

            if line_name not in processed_pipelines:
                point = Point(x_coord, y_coord)
                parsed_date = robust_date_parse(date_completed)
                new_feature = make_feature(line_name, parsed_date, psi, material, point, spatial_reference)

                if use_mongodb and gas_lines_collection is not None:
                    logger.debug(f"Inserting/updating feature in MongoDB: {line_name}")
                    upsert_mongodb_feature(
                        gas_lines_collection, line_name, date_completed, psi, material, point
                    )

                new_feature = align_feature_dtypes(new_feature, gas_lines_gdf)
                valid_rows = new_feature.dropna(how="all")

                if not valid_rows.empty:
                    logger.debug(f"Adding new feature from TXT: {line_name}")
                    gas_lines_gdf = pd.concat([gas_lines_gdf, valid_rows], ignore_index=True)
                    processed_pipelines.add(line_name)
                    features_added = True

        processed_reports.add(report_name)

    logger.info("Finished processing reports.")
    return processed_reports, gas_lines_gdf, features_added
