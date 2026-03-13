from typing import Dict, Any, Optional
import json
import re

def parse_tool_block(text: str) -> Optional[Dict[str, Any]]:
    """
    Finds the first <tool> ... </tool> block and parses it.
    Expects 'name: <tool_name>' and either an 'args: <json>' line
    or YAML-like 'key: value' lines.
    """
    if not text:
        return None

    match = re.search(r'<tool>\s*(.*?)\s*</tool>', text, re.DOTALL | re.IGNORECASE)
    if not match:
        return None

    block = match.group(1).strip()
    lines = block.split('\n')
    
    name = None
    args = {}
    
    # Try to find a single JSON arguments line first
    args_json_match = re.search(r'^args:\s*({.*})$', block, re.MULTILINE | re.DOTALL)
    if args_json_match:
        try:
            args = json.loads(args_json_match.group(1))
            # Find the name line
            name_match = re.search(r'^name:\s*(.+)$', block, re.MULTILINE)
            if name_match:
                name = name_match.group(1).strip()
            if name:
                return {"name": name, "arguments": args}
        except json.JSONDecodeError:
            pass # fallback to line-by-line

    # Fallback: Line by line (basic YAML-like)
    current_key = None
    current_val = []
    
    def save_current():
        if current_key and current_key != 'name':
            args[current_key] = '\n'.join(current_val).strip()
            
    for line in lines:
        if line.startswith('name:'):
            name = line.split(':', 1)[1].strip()
        elif ':' in line and not line.startswith(' '):
            save_current()
            k, v = line.split(':', 1)
            current_key = k.strip()
            if current_key == 'args':
                current_key = 'arguments' # normalize
            current_val = [v.strip()] if v.strip() else []
        else:
            if current_key:
                current_val.append(line)
                
    save_current()

    if name:
        # If args came through as a single 'arguments' string, try to parse it as JSON
        if 'arguments' in args and isinstance(args['arguments'], str):
            try:
                args = json.loads(args['arguments'])
            except json.JSONDecodeError:
                pass # keep as is
                
        return {"name": name, "arguments": args}

    return None
