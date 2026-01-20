# Audio

Reactive audio I/O with microphone, speaker, and WAV file support.

> **Optional dependency:** Install with `pip install rxplus[audio]`

## Design Intention

Audio components expose hardware I/O as reactive streams. `RxMicrophone` is an Observable emitting raw PCM chunks; `RxSpeaker` is an Observer consuming them. This allows building audio pipelines with standard Rx operators.

## API

```python
from rxplus import (
    RxMicrophone, RxSpeaker,
    create_wavfile, save_wavfile,
    PCMFormat,
)
```

### `PCMFormat`

Supported formats: `"UInt8"`, `"Int16"`, `"Int24"`, `"Int32"`, `"Float32"`

---

## Hardware I/O

### `RxMicrophone`

Subject that emits raw audio bytes from the microphone.

```python
mic = RxMicrophone(
    format="Float32",
    sample_rate=48_000,
    channels=1,
    frames_per_buffer=1024,
)

mic.subscribe(process_audio_chunk)
```

The stream automatically completes when the audio device stops.

### `RxSpeaker`

Subject that plays incoming audio bytes.

```python
speaker = RxSpeaker(
    format="Float32",
    sample_rate=48_000,
    channels=1,
)

audio_stream.subscribe(speaker)
```

---

## File I/O

### `create_wavfile(path, target_sample_rate, target_channels, target_format, frames_per_chunk)`

Observable that reads a WAV file and emits chunks as 2D NumPy arrays `[samples, channels]`.

```python
create_wavfile(
    "input.wav",
    target_sample_rate=48_000,
    target_channels=1,
    target_format="Float32",
    frames_per_chunk=1024,
).subscribe(speaker)
```

**Features:**

- Automatic resampling to target sample rate
- Channel conversion (downmix/upmix)
- Format conversion to target PCM type

### `save_wavfile(path, format, sample_rate, channels)`

Factory returning an Observer that writes audio chunks to a WAV file.

```python
mic.subscribe(save_wavfile("recording.wav", format="Float32", sample_rate=48_000))
```

---

## Example: Passthrough

```python
mic = RxMicrophone(format="Float32", sample_rate=48_000)
speaker = RxSpeaker(format="Float32", sample_rate=48_000)

mic.subscribe(speaker)  # Live audio passthrough
```
