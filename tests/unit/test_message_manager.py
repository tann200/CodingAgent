from src.core.orchestration.message_manager import MessageManager


def test_message_manager_truncation():
    # Set small token window so truncation happens quickly
    mm = MessageManager(max_tokens=20)

    # Append a system message (should be preserved if possible)
    mm.append('system', 'system initialization instructions')

    # Append user and assistant messages until we exceed window
    for i in range(10):
        mm.append('user', f'user message {i} ' + ('x'*50))
        mm.append('assistant', f'assistant reply {i} ' + ('y'*50))

    msgs = mm.all()
    # Ensure total tokens under limit
    total_tokens = sum(mm._estimate_tokens(m['content']) for m in msgs)
    assert total_tokens <= mm.max_tokens
    # Ensure last messages are preserved (assistant last)
    assert msgs[-1]['role'] == 'assistant'
    # System message should either be preserved or only removed if budget impossible
    roles = [m['role'] for m in msgs]
    assert 'assistant' in roles


def test_message_manager_preserves_recent():
    mm = MessageManager(max_tokens=50)
    for i in range(6):
        mm.append('user', 'short ' + str(i))
    # After appending, ensure at least the last 2 messages are present
    msgs = mm.all()
    assert len(msgs) >= 2
    assert msgs[-1]['content'].startswith('short')

