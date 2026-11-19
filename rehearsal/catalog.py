"""Which tools Synapse exposed, which of them change data, and strict argument checks.

Synapse's generated server types every argument as Any and pastes path
arguments straight into request URLs. Rehearsal closes both gaps without
editing the generated file: it reads each tool's HTTP method and route out of
the generated code itself (the code that actually runs), borrows real types
from the app's OpenAPI spec when one is available, and refuses arguments that
do not fit before any call is made.
"""
from __future__ import annotations

import ast
import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
READ_METHODS = {"GET", "HEAD", "OPTIONS"}


class ArgumentError(ValueError):
    """The agent sent arguments that do not fit the tool."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    method: str
    path: str
    schema: dict  # full JSON schema, used for validation
    agent_schema: dict  # simplified schema shown to the model

    @property
    def writes(self) -> bool:
        return self.method not in READ_METHODS

    @property
    def path_params(self) -> list[str]:
        return re.findall(r"{(\w+)}", self.path)

    def check(self, args: dict[str, Any] | None) -> dict[str, Any]:
        """Return cleaned arguments or raise ArgumentError with a message the agent can act on."""
        args = dict(args or {})
        unknown = set(args) - set(self.schema["properties"])
        if unknown:
            raise ArgumentError(f"{self.name} does not take {', '.join(sorted(unknown))}")
        for name in self.path_params:
            value = args.get(name)
            if value is None:
                raise ArgumentError(f"{name} is required")
            if isinstance(value, bool) or not SAFE_SEGMENT.match(str(value)):
                raise ArgumentError(f"{name} must be a plain identifier such as 5003, got {value!r}")
            if self.schema["properties"].get(name, {}).get("type") == "integer":
                if not str(value).isdigit() or int(value) < 1:
                    raise ArgumentError(f"{name} must be a positive whole number, got {value!r}")
                args[name] = int(value)
        errors = sorted(Draft202012Validator(self.schema).iter_errors(args), key=lambda e: list(e.path))
        if errors:
            where = ".".join(str(p) for p in errors[0].path) or "arguments"
            raise ArgumentError(f"{where}: {errors[0].message}")
        return args


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def routes_from_server(server_file: str | Path) -> dict[str, tuple[str, str]]:
    """Map tool name to (HTTP method, route) by parsing the Synapse-generated server."""
    tree = ast.parse(Path(server_file).read_text(encoding="utf-8"))
    routes: dict[str, tuple[str, str]] = {}
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        name = next((kw.value.value for d in fn.decorator_list if isinstance(d, ast.Call)
                     and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
                     for kw in d.keywords if kw.arg == "name" and isinstance(kw.value, ast.Constant)), None)
        if name is None:
            continue
        path = method = None
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.JoinedStr) and any(
                    isinstance(t, ast.Name) and t.id == "url" for t in node.targets):
                parts = node.value.values[1:]  # the first piece is os.environ["ENDPOINT_BASE_URL"]
                path = "".join(p.value if isinstance(p, ast.Constant) else
                               "{" + p.value.id + "}" if isinstance(p, ast.FormattedValue)
                               and isinstance(p.value, ast.Name) else "{?}" for p in parts)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "requests"
                    and node.func.attr in HTTP_METHODS):
                method = node.func.attr.upper()
        if path and method and "{?}" not in path:
            routes[name] = (method, path)
    return routes


def load(server_file: str | Path, mcp_tools: list, openapi: dict | None = None) -> dict[str, Tool]:
    """Build the catalogue from the MCP server's tool list plus the generated server's own code."""
    routes = routes_from_server(server_file)
    tools: dict[str, Tool] = {}
    for t in mcp_tools:
        if t.name not in routes:  # fail closed: a tool we cannot classify is never offered
            raise ValueError(f"Could not find the route for tool {t.name} in the generated server. "
                             "Rebuild it with: synapse build --discover")
        method, path = routes[t.name]
        op = ((openapi or {}).get("paths", {}).get(path, {}) or {}).get(method.lower())
        schema = _schema(t.inputSchema or {}, path, op, openapi or {})
        tools[t.name] = Tool(t.name, (t.description or "").strip(), method, path, schema, _for_agent(schema))
    return tools


def _resolve(node: Any, spec: dict, depth: int = 0) -> Any:
    if depth > 20:
        raise ValueError("OpenAPI $ref nesting is too deep")
    if isinstance(node, dict):
        if "$ref" in node:
            target: Any = spec
            for part in node["$ref"].lstrip("#/").split("/"):
                target = target[part]
            return _resolve(target, spec, depth + 1)
        return {k: _resolve(v, spec, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, spec, depth + 1) for v in node]
    return node


def _schema(mcp_schema: dict, path: str, op: dict | None, spec: dict) -> dict:
    props: dict[str, dict] = {name: {} for name in mcp_schema.get("properties", {})}
    required = set(re.findall(r"{(\w+)}", path))
    for name in required:
        props.setdefault(name, {})
    if op:
        for p in op.get("parameters", []):
            if p.get("in") == "path" and p["name"] in props:
                props[p["name"]] = {**_resolve(p.get("schema", {}), spec), "description": f"The {p['name']}"}
        body = (op.get("requestBody", {}).get("content", {}).get("application/json", {}) or {}).get("schema")
        if body:
            body = _resolve(body, spec)
            for name, s in body.get("properties", {}).items():
                if name in props:
                    props[name] = s
            required |= {r for r in body.get("required", []) if r in props}
    return {"type": "object", "properties": props, "required": sorted(required), "additionalProperties": False}


def _for_agent(schema: dict) -> dict:
    """Flatten optional and union types into the simple shapes models call most reliably."""
    out = copy.deepcopy(schema)
    for name, prop in out["properties"].items():
        if "anyOf" in prop:
            options = [o for o in prop.pop("anyOf") if o.get("type") != "null"]
            pick = next((o for o in options if o.get("type") in ("number", "integer")), options[0] if options else {})
            prop.update({k: v for k, v in pick.items() if k not in prop})
        prop.pop("title", None)
        out["properties"][name] = prop
    return out
