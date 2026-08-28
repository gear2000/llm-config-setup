#!/usr/bin/env python3
"""Semantic validator for native, uncompressed Draw.io / diagrams.net XML."""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


class ValidationError(ValueError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children_named(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local(child.tag) == name]


def _has_geometry(cell: ET.Element) -> bool:
    return any(
        _local(child.tag) == "mxGeometry" and child.get("as") == "geometry"
        for child in list(cell)
    )


def validate_drawio(path: Path, *, xsd: Path | None = None) -> list[str]:
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise ValidationError(f"XML is not well formed: {exc}") from exc
    root = tree.getroot()
    if _local(root.tag) != "mxfile":
        raise ValidationError("root element must be mxfile")
    diagrams = _children_named(root, "diagram")
    if not diagrams:
        raise ValidationError("mxfile must contain at least one diagram")

    errors: list[str] = []
    for index, diagram in enumerate(diagrams, start=1):
        models = _children_named(diagram, "mxGraphModel")
        if len(models) != 1:
            errors.append(f"diagram {index}: expected exactly one mxGraphModel")
            continue
        model_root = next((c for c in _children_named(models[0], "root")), None)
        if model_root is None:
            errors.append(f"diagram {index}: mxGraphModel missing root")
            continue
        cells = [c for c in list(model_root) if _local(c.tag) == "mxCell"]
        ids: dict[str, ET.Element] = {}
        duplicates: set[str] = set()
        for cell in cells:
            cell_id = cell.get("id")
            if not cell_id:
                errors.append(f"diagram {index}: mxCell missing id")
                continue
            if cell_id in ids:
                duplicates.add(cell_id)
            ids[cell_id] = cell
        for dup in sorted(duplicates):
            errors.append(f"diagram {index}: duplicate id {dup}")
        if "0" not in ids:
            errors.append(f"diagram {index}: missing structural cell id=0")
        if "1" not in ids:
            errors.append(f"diagram {index}: missing structural cell id=1")
        elif ids["1"].get("parent") != "0":
            errors.append(f"diagram {index}: structural cell id=1 must have parent 0")
        for cell_id, cell in sorted(ids.items()):
            parent = cell.get("parent")
            if parent and parent not in ids:
                errors.append(
                    f"diagram {index}: cell {cell_id} has missing parent {parent}"
                )
            if cell.get("vertex") == "1" and not _has_geometry(cell):
                errors.append(f"diagram {index}: vertex {cell_id} missing geometry")
            if cell.get("edge") == "1":
                if not _has_geometry(cell):
                    errors.append(f"diagram {index}: edge {cell_id} missing geometry")
                for attr in ("source", "target"):
                    ref = cell.get(attr)
                    if ref and ref not in ids:
                        errors.append(
                            f"diagram {index}: edge {cell_id} has missing {attr} {ref}"
                        )
    if xsd is not None:
        try:
            import xmlschema  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ValidationError(
                "XSD validation requested but xmlschema is not installed"
            ) from exc
        schema = xmlschema.XMLSchema(str(xsd))
        if not schema.is_valid(str(path)):
            for err in schema.iter_errors(str(path)):
                errors.append(f"xsd: {err.reason}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--xsd", type=Path, help="Optional schema file for structural validation"
    )
    args = parser.parse_args(argv)
    try:
        errors = validate_drawio(args.path, xsd=args.xsd)
    except ValidationError as exc:
        print(f"drawio validation failed: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"drawio validation failed: {error}", file=sys.stderr)
        return 1
    print(f"drawio validation passed: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
