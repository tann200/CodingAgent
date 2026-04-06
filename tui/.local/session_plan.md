# Objective
Implement T_DIFF, T_SETTINGS, and T_SIDEBAR (T001–T006 completed).

# Tasks

### T_DIFF: Side-by-side diff viewer — IN PROGRESS
- **Files**: src/ui/components/diff_viewer.py (new), components/__init__.py, app.py, app.tcss
- Parse unified diff into left/right column pairs; Horizontal two-panel render with Accept/Reject
- Replace unified-text Static in handle_diff_preview

### T_SETTINGS: Settings screen overhaul — IN PROGRESS
- **Files**: src/ui/features/settings/screen.py
- Reactive model filtering on provider Select.Changed
- API Keys section: status dot + provider name + password input + Test button

### T_SIDEBAR: Sidebar expansion — IN PROGRESS
- **Files**: src/ui/bus.py, core_bridge.py, mock_engine.py, app.py, app.tcss
- GitBranchEvent + subscription + mock emission
- New widgets: SESSION COST, TOKEN BREAKDOWN, GIT, TOOLS CALLED; width 38→44

### T_VERIFY: Restart + check — PENDING [T_DIFF, T_SETTINGS, T_SIDEBAR]
