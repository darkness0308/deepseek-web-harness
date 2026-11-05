"""Bounded JSON results that retain exit codes and machine-readable metadata."""
import copy
import json


def encode_result(result, limit):
    value = copy.deepcopy(result)
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded) <= limit:
        return encoded
    value["truncated"] = True
    # Clip string values first, keeping their keys and essential numeric status.
    protected = {"id", "epoch", "name", "path", "destination", "sha256", "fingerprint", "error", "license", "type"}
    def strings(node, parent_key=None):
        found = []
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(child, str) and key not in protected:
                    found.append((node, key, child))
                elif not isinstance(child, str):
                    found.extend(strings(child, key))
        elif isinstance(node, list):
            for i, child in enumerate(node):
                if isinstance(child, str) and parent_key not in ("files",):
                    found.append((node, i, child))
                elif not isinstance(child, str):
                    found.extend(strings(child, parent_key))
        return found
    while len(json.dumps(value, ensure_ascii=False)) > limit:
        candidates = strings(value)
        candidates = [item for item in candidates if item[2]]
        if candidates:
            parent, key, text = max(candidates, key=lambda item: len(item[2]))
            excess = len(json.dumps(value, ensure_ascii=False)) - limit
            parent[key] = text[:max(0, len(text) - max(excess, len(text) // 4, 1))]
            continue
        # Many result rows can exceed the limit even with empty strings.
        arrays = [(k, v) for k, v in value.items() if isinstance(v, list) and v]
        if arrays:
            key, array = max(arrays, key=lambda item: len(item[1]))
            array.pop()
            value["omitted_items"] = value.get("omitted_items", 0) + 1
            continue
        break
    return json.dumps(value, ensure_ascii=False)
