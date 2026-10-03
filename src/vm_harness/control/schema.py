"""Turning Python signatures into JSON Schema, JSON arguments into Python values, and results back into JSON.

Operations in the catalog are generated from the backends' own methods, so their parameter schemas come from type
hints and defaults: a new backend method gets a correct schema without anyone writing one.
"""
from __future__ import annotations

import base64
import dataclasses
import enum
import inspect
import typing
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional, Union, get_args, get_origin, get_type_hints

_SKIP_PARAMS = {"self", "cls"}


def type_schema(tp: Any, depth: int = 0) -> dict[str, Any]:
    """JSON Schema for a type hint (dataclasses become objects, enums become string enums)."""
    if tp is inspect.Parameter.empty or tp is Any or depth > 6:
        return {}
    origin = get_origin(tp)
    if origin is Union or (origin is not None and str(origin) == "types.UnionType"):
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return type_schema(args[0], depth + 1)
        return {"anyOf": [type_schema(a, depth + 1) for a in args]}
    if origin in (list, tuple, set, frozenset, typing.Sequence, typing.Iterable):
        args = get_args(tp)
        return {"type": "array", "items": type_schema(args[0], depth + 1) if args else {}}
    if origin in (dict, typing.Mapping):
        args = get_args(tp)
        return {"type": "object", "additionalProperties": type_schema(args[1], depth + 1) if len(args) == 2 else {}}
    if origin is typing.Literal:
        return {"enum": list(get_args(tp))}
    if isinstance(tp, type):
        if issubclass(tp, bool):
            return {"type": "boolean"}
        if issubclass(tp, enum.Enum):
            return {"type": "string", "enum": [m.value for m in tp]}
        if issubclass(tp, int):
            return {"type": "integer"}
        if issubclass(tp, float):
            return {"type": "number"}
        if issubclass(tp, (str, Path)):
            return {"type": "string"}
        if issubclass(tp, bytes):
            return {"type": "string", "contentEncoding": "base64"}
        if dataclasses.is_dataclass(tp):
            return dataclass_schema(tp, depth + 1)
        if issubclass(tp, (list, tuple, set)):
            return {"type": "array"}
        if issubclass(tp, dict):
            return {"type": "object"}
    return {}


def dataclass_schema(cls: type, depth: int = 0) -> dict[str, Any]:
    hints = _hints(cls)
    props: dict[str, Any] = {}
    required: list[str] = []
    for f in dataclasses.fields(cls):
        s = dict(type_schema(hints.get(f.name, f.type), depth))
        if f.default is not dataclasses.MISSING:
            s["default"] = to_json(f.default)
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            s["default"] = to_json(f.default_factory())  # type: ignore[misc]
        else:
            required.append(f.name)
        props[f.name] = s
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def _hints(obj: Any) -> dict[str, Any]:
    try:
        return get_type_hints(obj)
    except Exception:  # noqa: BLE001 - forward references we cannot resolve fall back to raw annotations
        return dict(getattr(obj, "__annotations__", {}) or {})


def signature_schema(fn: Callable, *, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    """An object schema for a function's parameters (minus `skip`)."""
    sig = inspect.signature(fn)
    hints = _hints(fn)
    props: dict[str, Any] = {}
    required: list[str] = []
    for name, p in sig.parameters.items():
        if name in _SKIP_PARAMS or name in skip or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        s = dict(type_schema(hints.get(name, p.annotation)))
        if p.default is inspect.Parameter.empty:
            required.append(name)
        elif p.default is not None:
            s["default"] = to_json(p.default)
        props[name] = s
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def coerce(value: Any, tp: Any) -> Any:
    """A JSON value converted to the type a parameter expects (dict -> dataclass, str -> enum, base64 -> bytes)."""
    if value is None or tp is inspect.Parameter.empty or tp is Any:
        return value
    origin = get_origin(tp)
    if origin is Union or (origin is not None and str(origin) == "types.UnionType"):
        for a in get_args(tp):
            if a is type(None):
                continue
            try:
                return coerce(value, a)
            except (TypeError, ValueError):
                continue
        return value
    if origin in (list, tuple, set) and isinstance(value, (list, tuple)):
        args = get_args(tp)
        items = [coerce(v, args[0]) for v in value] if args else list(value)
        return origin(items) if origin is not list else items
    if isinstance(tp, type):
        if issubclass(tp, enum.Enum):
            return value if isinstance(value, tp) else tp(value)
        if dataclasses.is_dataclass(tp) and isinstance(value, dict):
            hints = _hints(tp)
            names = {f.name for f in dataclasses.fields(tp)}
            unknown = set(value) - names
            if unknown:
                raise ValueError(f"unknown field(s) for {tp.__name__}: {', '.join(sorted(unknown))}")
            return tp(**{k: coerce(v, hints.get(k)) for k, v in value.items()})
        if issubclass(tp, bool) and isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        if issubclass(tp, int) and not isinstance(value, bool) and isinstance(value, (str, float)):
            return int(value)
        if issubclass(tp, float) and isinstance(value, (str, int)):
            return float(value)
        if issubclass(tp, bytes) and isinstance(value, str):
            return base64.b64decode(value)
        if issubclass(tp, Path) and isinstance(value, str):
            return Path(value)
    return value


def bind(fn: Callable, args: dict[str, Any], *, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    """Keyword arguments for `fn` from JSON `args`, converted, with unknown or missing arguments reported clearly."""
    sig = inspect.signature(fn)
    if any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()) and len(sig.parameters) == 1:
        return dict(args)          # a pass-through: whoever receives the arguments validates them
    hints = _hints(fn)
    known = {n for n in sig.parameters if n not in _SKIP_PARAMS and n not in skip}
    unknown = set(args) - known
    if unknown:
        raise ValueError(f"unknown argument(s): {', '.join(sorted(unknown))}; expected {', '.join(sorted(known)) or 'none'}")
    out: dict[str, Any] = {}
    for name, p in sig.parameters.items():
        if name not in known:
            continue
        if name in args:
            out[name] = coerce(args[name], hints.get(name, p.annotation))
        elif p.default is inspect.Parameter.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise ValueError(f"missing required argument: {name}")
    return out


def to_json(value: Any, depth: int = 0) -> Any:
    """Anything a backend returns, as plain JSON (dataclasses, enums, bytes as base64, paths, datetimes)."""
    if depth > 12:
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_json(getattr(value, f.name), depth + 1) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_json(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json(v, depth + 1) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii"), "bytes": len(value)}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    for attr in ("to_dict", "dict", "model_dump"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return to_json(fn(), depth + 1)
            except TypeError:
                pass
    if hasattr(value, "__dict__"):
        return {k: to_json(v, depth + 1) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def first_line(doc: Optional[str]) -> str:
    return (inspect.cleandoc(doc).splitlines() or [""])[0].strip() if doc else ""
