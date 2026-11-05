"""Validate tool schemas locally; validation never fetches remote references."""
from jsonschema import Draft202012Validator


def check_schema(schema):
    if not isinstance(schema, dict):
        raise ValueError("Tool parameters must be a schema object")
    Draft202012Validator.check_schema(schema)
    pending = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                if key in node and (not isinstance(node[key], str) or not node[key].startswith("#")):
                    raise ValueError("Tool schemas support local references only")
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
