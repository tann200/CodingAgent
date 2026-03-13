from pathlib import Path
from src.ui.app import CodingAgentApp, AppConfig
from src.core.orchestration.event_bus import EventBus


def test_app_wires_telemetry(tmp_path):
    cfg = AppConfig(working_dir=str(tmp_path), telemetry_enabled=True, telemetry_path=str(tmp_path / 'telemetry_app.jsonl'))
    app = CodingAgentApp(config=cfg)
    # publish an event and ensure telemetry file is written
    app.event_bus.publish('message.truncation', {'dropped_count': 1, 'dropped_tokens': 10, 'tokens_after': 5})
    # read file
    out = Path(cfg.telemetry_path)
    assert out.exists()
    text = out.read_text(encoding='utf-8')
    assert 'message.truncation' in text

