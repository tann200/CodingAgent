from src.tools.tools_config import PermissionLevel, TOOL_PERMISSIONS


def test_network_tools_are_not_marked_read_only():
    assert TOOL_PERMISSIONS["read_web_page"] == PermissionLevel.DANGER
    assert TOOL_PERMISSIONS["web_search"] == PermissionLevel.DANGER
