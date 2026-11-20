"""Bounded sample-addressed audio storage and stateful resampling."""

import numpy as np


class AudioRing:
    def __init__(self, capacity=16000 * 120):
        self.data = np.zeros(capacity, dtype=np.float32)
        self.end = 0

    @property
    def start(self):
        return max(0, self.end - len(self.data))

    def append(self, samples):
        samples = np.asarray(samples, dtype=np.float32)
        original_length = len(samples)
        samples = samples[-len(self.data):]
        begin = (self.end + original_length - len(samples)) % len(self.data)
        first = min(len(samples), len(self.data) - begin)
        self.data[begin:begin + first] = samples[:first]
        self.data[:len(samples) - first] = samples[first:]
        self.end += original_length

    def read(self, start, end):
        if start < self.start or end > self.end or end < start:
            raise ValueError("audio range is no longer available")
        indices = np.arange(start, end, dtype=np.int64) % len(self.data)
        return self.data[indices].copy()


class StreamingResampler:
    def __init__(self, source_rate, target_rate=16000):
        self.stream = None
        if source_rate != target_rate:
            import soxr

            self.stream = soxr.ResampleStream(source_rate, target_rate, 1, dtype="float32", quality="HQ")

    def feed(self, samples):
        audio = np.asarray(samples, dtype=np.float32)
        return self.stream.resample_chunk(audio) if self.stream is not None else audio
