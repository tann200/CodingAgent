#!/usr/bin/env python3
import time
from src.core.llm_manager import _provider_manager
from src.core.orchestration.event_bus import EventBus
print('initial pm._event_bus', getattr(_provider_manager, '_event_bus', None))
eb = EventBus()
def on_start(p):
    print('received startup event', p)

print('subscribe to bus')
eb.subscribe('orchestrator.startup', on_start)
print('set pm event bus')
_provider_manager.set_event_bus(eb)
print('pm._event_bus set to', getattr(_provider_manager, '_event_bus', None))

from importlib import reload
import src.core.orchestration.orchestrator as orch_mod
reload(orch_mod)
from src.core.orchestration.orchestrator import Orchestrator
print('creating Orchestrator')
o = Orchestrator()
print('created orchestrator')
print('waiting')
time.sleep(1)
print('done')

