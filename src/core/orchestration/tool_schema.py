from typing import Optional, Dict, Any
from pydantic import BaseModel


class ToolContract(BaseModel):
    tool: str
    args: Dict[str, Any]
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

