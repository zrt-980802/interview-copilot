from copilot.audio import SimulatedAudioInput


def test_simulated_input_is_queue_backed_and_preserves_pcm_bytes():
    source = SimulatedAudioInput(sample_rate=16000, channels=1)
    payload = b"pcm16"
    source.push(payload)
    assert source.get_chunk(timeout=0.1) == payload
    assert source.sample_rate == 16000
    assert source.channels == 1


def test_simulated_input_returns_none_when_stopped():
    source = SimulatedAudioInput()
    source.close()
    assert source.get_chunk(timeout=0.01) is None
