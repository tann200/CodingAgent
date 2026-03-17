from typing import Dict, Type, Any, Optional
from pydantic import BaseModel, Field

# Edit size limits
MAX_PATCH_LINES = 200
MAX_FILE_SIZE_BYTES = 1024 * 1024  # 1MB

# Simple registry for per-tool pydantic contracts
_TOOL_CONTRACTS: Dict[str, Type[BaseModel]] = {}


def register_tool_contract(name: str, model: Type[BaseModel]) -> None:
    _TOOL_CONTRACTS[name] = model


def get_tool_contract(name: str):
    return _TOOL_CONTRACTS.get(name)


class ToolContract(BaseModel):
    tool: str = Field(..., description="The name of the tool.")
    args: Dict[str, Any] = Field(..., description="The arguments passed to the tool.")
    result: Optional[Dict[str, Any]] = Field(
        None, description="The result of the tool execution."
    )
    error: Optional[str] = Field(
        None, description="Any error that occurred during tool execution."
    )


# Edit Size Guard
def validate_patch_size(patch: str) -> Dict[str, Any]:
    """Validate patch doesn't exceed size limits.

    Returns:
        {"status": "ok"} if valid
        {"status": "error", "error": "...", "requires_split": True} if too large
    """
    if not patch:
        return {"status": "error", "error": "Empty patch"}

    added_lines = patch.count("+")
    removed_lines = patch.count("-")
    total_changes = added_lines + removed_lines

    if total_changes > MAX_PATCH_LINES:
        return {
            "status": "error",
            "error": f"Patch too large ({total_changes} lines). Max allowed: {MAX_PATCH_LINES}. Split into smaller edits.",
            "requires_split": True,
            "added_lines": added_lines,
            "removed_lines": removed_lines,
        }

    return {"status": "ok"}


def validate_file_size(file_path: str, content: str) -> Dict[str, Any]:
    """Validate file content doesn't exceed size limits."""
    size_bytes = len(content.encode("utf-8"))

    if size_bytes > MAX_FILE_SIZE_BYTES:
        return {
            "status": "error",
            "error": f"File too large ({size_bytes} bytes). Max allowed: {MAX_FILE_SIZE_BYTES} bytes.",
            "size_bytes": size_bytes,
            "max_bytes": MAX_FILE_SIZE_BYTES,
        }

    return {"status": "ok"}


# Example simple contracts (importable)
class ReadFileContract(BaseModel):
    path: str
    status: str
    content: Optional[str] = None


class WriteFileContract(BaseModel):
    path: str
    status: str


# Register example contracts for common file tools
try:
    register_tool_contract("read_file", ReadFileContract)
    register_tool_contract("write_file", WriteFileContract)
    register_tool_contract("edit_file", WriteFileContract)

    # Additional contracts
    class GeneratePatchContract(BaseModel):
        path: str
        patch: str

    class ApplyPatchContract(BaseModel):
        path: str
        status: str

    class RunTestsContract(BaseModel):
        status: str
        returncode: int

    register_tool_contract("generate_patch", GeneratePatchContract)
    register_tool_contract("apply_patch", ApplyPatchContract)
    register_tool_contract("run_tests", RunTestsContract)
except Exception:
    pass
