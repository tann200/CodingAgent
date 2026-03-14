from typing import Dict, Type, Any, Optional
from pydantic import BaseModel, Field

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
except Exception:
    pass
