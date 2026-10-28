// Raw floating point microphone frames, sent to main thread every ~0.5 seconds.
// Keeping capture separate from network requests prevents stalls from disrupting audio.
class PCMRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(Math.max(4096, Math.round(sampleRate / 2)));
    this.offset = 0;
    this.port.onmessage = event => {
      if (event.data?.type === 'flush') {
        this.flush();
        this.port.postMessage({ type: 'flushed' });
      }
    };
  }
  flush() {
    if (!this.offset) return;
    const data = this.buffer.slice(0, this.offset);
    this.port.postMessage({ type: 'audio', data }, [data.buffer]);
    this.offset = 0;
  }
  process(inputs, outputs) {
    const input = inputs[0];
    if (input?.length) {
      const channel = input[0];
      let at = 0;
      while (at < channel.length) {
        const n = Math.min(channel.length - at, this.buffer.length - this.offset);
        this.buffer.set(channel.subarray(at, at + n), this.offset);
        at += n;
        this.offset += n;
        if (this.offset === this.buffer.length) this.flush();
      }
    }
    for (const output of outputs) for (const ch of output) ch.fill(0);
    return true;
  }
}
registerProcessor('pcm-recorder', PCMRecorder);