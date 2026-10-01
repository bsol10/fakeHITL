from src.backend.repoagent import parse_message_with_ai


def test_repoagent():
    # Sample text to test if the AI agent returns a result
    sample_text = """
    A 2026 research report found that digital literacy skills are
    becoming increasingly important for young people entering employment.
    """

    result = parse_message_with_ai(sample_text)

    # Check the output while testing
    print(result)

    # The agent should return something instead of None
    assert result is not None
