from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.orm import Session

from load.sales_base_loader import load_module_rows, process_source_records
from load.module_loader_config import MODULE_LOADER_CONFIG


def load_rows_by_module(
    session: Session,
    module_name: str,
    rows: List[Dict[str, Any]],
    chunk_size: int = 500,
) -> Dict[str, int]:
    config = MODULE_LOADER_CONFIG[module_name]

    return load_module_rows(
        session=session,
        model=config["model"],
        rows=rows,
        allowed_columns=config["allowed_columns"],
        required_fields=config["required_fields"],
        update_columns=config["update_columns"],
        decimal_columns=config["decimal_columns"],
        chunk_size=chunk_size,
        context=config["context"],
    )


def process_records_by_module(
    session: Session,
    module_name: str,
    source_records: List[Dict[str, Any]],
    transform_func,
    chunk_size: int = 500,
) -> Dict[str, Any]:
    config = MODULE_LOADER_CONFIG[module_name]

    return process_source_records(
        session=session,
        source_records=source_records,
        transform_func=transform_func,
        model=config["model"],
        allowed_columns=config["allowed_columns"],
        required_fields=config["required_fields"],
        update_columns=config["update_columns"],
        decimal_columns=config["decimal_columns"],
        chunk_size=chunk_size,
        context=config["context"],
        gl_module_name=module_name,
    )