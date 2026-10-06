"""Tool contract export and schema hash generation."""

import hashlib
import json
from pathlib import Path
from typing import Any
from mcp.server.mcpserver import MCPServer


def canonical_json_str(data: Any) -> str:
    """Deterministic JSON string for hashing and snapshots."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def export_tools_contract(mcp: MCPServer) -> list[dict[str, Any]]:
    """Export canonical list of registered tools with schemas and annotations."""
    tools = await mcp.list_tools()
    out = []
    for t in sorted(tools, key=lambda x: x.name):
        annotations_dict = t.annotations.model_dump(by_alias=True) if t.annotations else None
        out.append(
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "annotations": annotations_dict,
            }
        )
    return out


def compute_contract_hash(contract: list[dict[str, Any]]) -> str:
    """Compute sha256 hash of canonical tool contract."""
    canonical_repr = canonical_json_str(contract)
    return hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()


async def save_contract_snapshot(
    mcp: MCPServer, path: Path | str = "contract/tools_schema.json"
) -> tuple[str, Path]:
    """Save contract snapshot to file and return (schema_hash, path)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    contract = await export_tools_contract(mcp)
    schema_hash = compute_contract_hash(contract)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(contract, f, indent=2, sort_keys=True)
    return schema_hash, p
