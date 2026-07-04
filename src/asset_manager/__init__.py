"""Asset Manager — automated media asset scanning, annotation, and selection.

Usage:
    from src.asset_manager import (
        scan_asset_directory,
        batch_annotate,
        find_new_assets,
        upsert_annotations,
        load_index,
        select_assets,
        quick_select_by_quality,
        get_all_summaries,
        count_by_type,
        AssetSelection,
    )
"""

from .annotator import annotate_asset, batch_annotate
from .index_store import (
    count_by_type,
    find_new_assets,
    get_all_summaries,
    load_index,
    upsert_annotations,
)
from .models import AssetSelection
from .scanner import scan_asset_directory
from .selector import quick_select_by_quality, select_assets

__all__ = [
    "annotate_asset",
    "AssetSelection",
    "batch_annotate",
    "count_by_type",
    "find_new_assets",
    "get_all_summaries",
    "load_index",
    "quick_select_by_quality",
    "scan_asset_directory",
    "select_assets",
    "upsert_annotations",
]
