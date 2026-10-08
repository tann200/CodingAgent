import logging
from typing import Dict, Type, Any, Optional, List
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

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
    status: str = ""
    content: Optional[str] = None


class WriteFileContract(BaseModel):
    path: str
    content: str = ""
    status: str = ""


class GeneratePatchContract(BaseModel):
    path: str
    patch: str


class ApplyPatchContract(BaseModel):
    path: str
    status: str = ""


class RunTestsContract(BaseModel):
    status: str = ""
    returncode: int = 0


class BashContract(BaseModel):
    command: str = ""
    status: str = ""
    returncode: int = 0
    stdout: Optional[str] = None
    stderr: Optional[str] = None


class GlobContract(BaseModel):
    pattern: str
    status: str = ""
    matches: List[str] = []


class GrepContract(BaseModel):
    pattern: str
    status: str = ""
    matches: List[str] = []


# Register example contracts for common file tools
try:
    register_tool_contract("read_file", ReadFileContract)
    register_tool_contract("write_file", WriteFileContract)
    register_tool_contract("edit_file", WriteFileContract)
    register_tool_contract("edit_file_atomic", WriteFileContract)

    register_tool_contract("generate_patch", GeneratePatchContract)
    register_tool_contract("apply_patch", ApplyPatchContract)

    register_tool_contract("run_tests", RunTestsContract)

    register_tool_contract("bash", BashContract)
    register_tool_contract("glob", GlobContract)
    register_tool_contract("grep", GrepContract)
except Exception as _e:
    logger.warning("tool_contracts: registration failed: %s", _e)
