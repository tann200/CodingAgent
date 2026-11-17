import logging
from typing import ClassVar, Dict, Type, Any, Optional, List
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


class ReadFileInputContract(BaseModel):
    path: str


class ReadFileOutputContract(BaseModel):
    path: Optional[str] = None
    status: str = ""
    ok: Optional[bool] = None
    content: Optional[str] = None
    error: Optional[str] = None


class ReadFileContract(BaseModel):
    input_model: ClassVar[Type[BaseModel]] = ReadFileInputContract
    output_model: ClassVar[Type[BaseModel]] = ReadFileOutputContract
    path: str
    status: str = ""
    content: Optional[str] = None


class WriteFileInputContract(BaseModel):
    path: str
    content: Optional[str] = ""
    patch: Optional[str] = None
    old_string: Optional[str] = None
    new_string: Optional[str] = None


class WriteFileOutputContract(BaseModel):
    path: Optional[str] = None
    status: str = ""
    ok: Optional[bool] = None
    error: Optional[str] = None


class WriteFileContract(BaseModel):
    input_model: ClassVar[Type[BaseModel]] = WriteFileInputContract
    output_model: ClassVar[Type[BaseModel]] = WriteFileOutputContract
    path: str
    content: str = ""
    status: str = ""


class GeneratePatchInputContract(BaseModel):
    path: str
    new_content: str


class GeneratePatchOutputContract(BaseModel):
    status: str = ""
    ok: Optional[bool] = None
    patch: Optional[str] = None
    error: Optional[str] = None


class GeneratePatchContract(BaseModel):
    input_model: ClassVar[Type[BaseModel]] = GeneratePatchInputContract
    output_model: ClassVar[Type[BaseModel]] = GeneratePatchOutputContract
    path: Optional[str] = None
    new_content: Optional[str] = None
    patch: Optional[str] = None


class ApplyPatchInputContract(BaseModel):
    path: str
    patch: str


class ApplyPatchOutputContract(BaseModel):
    path: Optional[str] = None
    status: str = ""
    ok: Optional[bool] = None
    error: Optional[str] = None


class ApplyPatchContract(BaseModel):
    input_model: ClassVar[Type[BaseModel]] = ApplyPatchInputContract
    output_model: ClassVar[Type[BaseModel]] = ApplyPatchOutputContract
    path: str
    status: str = ""


class RunTestsInputContract(BaseModel):
    pass


class RunTestsOutputContract(BaseModel):
    status: str = ""
    ok: Optional[bool] = None
    returncode: int = 0
    error: Optional[str] = None


class RunTestsContract(BaseModel):
    input_model: ClassVar[Type[BaseModel]] = RunTestsInputContract
    output_model: ClassVar[Type[BaseModel]] = RunTestsOutputContract
    status: str = ""
    returncode: int = 0


class BashInputContract(BaseModel):
    command: str


class BashOutputContract(BaseModel):
    command: str = ""
    status: str = ""
    ok: Optional[bool] = None
    returncode: int = 0
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    error: Optional[str] = None


class BashContract(BaseModel):
    input_model: ClassVar[Type[BaseModel]] = BashInputContract
    output_model: ClassVar[Type[BaseModel]] = BashOutputContract
    command: str = ""
    status: str = ""
    returncode: int = 0
    stdout: Optional[str] = None
    stderr: Optional[str] = None


class GlobInputContract(BaseModel):
    pattern: str


class GlobOutputContract(BaseModel):
    status: str = ""
    ok: Optional[bool] = None
    matches: List[Any] = []
    error: Optional[str] = None


class GlobContract(BaseModel):
    input_model: ClassVar[Type[BaseModel]] = GlobInputContract
    output_model: ClassVar[Type[BaseModel]] = GlobOutputContract
    pattern: str
    status: str = ""
    matches: List[Any] = []


class GrepInputContract(BaseModel):
    pattern: str


class GrepOutputContract(BaseModel):
    status: str = ""
    ok: Optional[bool] = None
    output: Optional[Any] = None
    matches: List[Any] = []
    error: Optional[str] = None


class GrepContract(BaseModel):
    input_model: ClassVar[Type[BaseModel]] = GrepInputContract
    output_model: ClassVar[Type[BaseModel]] = GrepOutputContract
    pattern: str
    status: str = ""
    matches: List[Any] = []


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
