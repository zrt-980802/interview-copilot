"""Exercise the configured STT with known speech and synthetic nonspeech, without recording."""

import argparse
import io
import json
from pathlib import Path
import re
import sys
import threading
import time
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT / "src")]

from copilot.audio import _resample_to_16k
from copilot.hotwords import build_hotwords
from copilot.stt import STTClient
from interview_copilot.services.corpus import CorpusIndex
from interview_copilot.services.documents import DocumentLibrary


def wav_bytes(samples):
    output = io.BytesIO()
    with wave.open(output,"wb") as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(16000)
        handle.writeframes((np.clip(samples,-1,1) * 32767).astype(np.int16).tobytes())
    return output.getvalue()


def normalized(text):
    return re.sub(r"[^\w\u3400-\u9fff]","",text).casefold()


def character_error_rate(expected,actual):
    expected,actual = normalized(expected),normalized(actual)
    previous = list(range(len(actual) + 1))
    for row,left in enumerate(expected,1):
        current = [row]
        for column,right in enumerate(actual,1):
            current.append(min(current[-1] + 1,previous[column] + 1,previous[column - 1] + (left != right)))
        previous = current
    return previous[-1] / max(1,len(expected))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--speech-wav",default="data/stt-synthetic-check.wav")
    parser.add_argument("--expected",default="这是一次本地语音转写测试，我们正在检查耳机和麦克风。")
    parser.add_argument("--output",default="data/stt-quality-20261118.json")
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    with wave.open(str(ROOT / args.speech_wav),"rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError("validation speech must use PCM16")
        samples = np.frombuffer(handle.readframes(handle.getnframes()),dtype=np.int16).astype(np.float32)
        samples = samples.reshape(-1,handle.getnchannels()).mean(axis=1) / 32768
        speech = _resample_to_16k(samples,handle.getframerate())
    index = CorpusIndex()
    workers = type("NoRecordingWorkers",(),{"cancelled":threading.Event()})()
    library = DocumentLibrary(ROOT / "data/documents",index,workers)
    resume = library.resume()
    prompt = build_hotwords(resume_text=resume[0] if resume else "")
    random = np.random.default_rng(730)
    length = 16000 * 3
    cases = [
        ("silence",np.zeros(length),""),
        ("white_noise",random.normal(0,0.012,length),""),
        ("steady_hum",0.025 * np.sin(2 * np.pi * 180 * np.arange(length) / 16000),""),
        ("clean_speech",speech,args.expected),
        ("quiet_speech_with_noise",speech * 0.2 + random.normal(0,0.002,len(speech)),args.expected),
    ]
    output = ROOT / args.output; output.parent.mkdir(parents=True,exist_ok=True)
    report = {"model":config["stt"]["model"],"prompt_chars":len(prompt),"cases":[]}
    client = STTClient({**config["stt"],"base_url":config["stt"]["url"],"timeout":90})
    try:
        for name,samples,expected in cases:
            for label,hint in (("none",""),("hotwords",prompt)):
                started = time.monotonic()
                text = client.transcribe(wav_bytes(samples),prompt=hint,language="zh")
                result = {"name":name,"prompt":label,"text":text,"seconds":round(time.monotonic()-started,3),
                          "nonspeech":not bool(expected)}
                result.update({"character_error_rate":round(character_error_rate(expected,text),3)} if expected else {"hallucinated":bool(text)})
                report["cases"].append(result)
                output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
                print(json.dumps(result,ensure_ascii=False),flush=True)
    finally:
        client.close(); index.close()
    print("STT report: " + str(output),flush=True)


if __name__ == "__main__":
    main()
