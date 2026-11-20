// HomePlay sound player. Receives 16-bit PCM chunks from the page and plays them with a
// small jitter buffer (~60 ms). Resamples if the PC's sample rate differs from the device's.
class HomePlayPcm extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = 96000;                       // ring buffer capacity in frames
    this.left = new Float32Array(this.size);
    this.right = new Float32Array(this.size);
    this.readPos = 0;
    this.writePos = 0;
    this.count = 0;                          // frames waiting to be played
    this.frac = 0;                           // position between readPos and readPos + 1
    this.rate = sampleRate;                  // sample rate of the incoming audio
    this.playing = false;
    this.port.onmessage = (e) => this.push(e.data);
  }

  push({ pcm, channels, rate }) {
    if (rate > 0) this.rate = rate;
    const ch = Math.max(1, channels | 0);
    const frames = Math.floor(pcm.length / ch);
    for (let i = 0; i < frames; i++) {
      if (this.count >= this.size) {         // overflow: forget the oldest sample
        this.readPos = (this.readPos + 1) % this.size;
        this.count--;
      }
      const l = pcm[i * ch] / 32768;
      this.left[this.writePos] = l;
      this.right[this.writePos] = ch > 1 ? pcm[i * ch + 1] / 32768 : l;
      this.writePos = (this.writePos + 1) % this.size;
      this.count++;
    }
  }

  process(inputs, outputs) {
    const out = outputs[0];
    const L = out[0];
    const R = out.length > 1 ? out[1] : null;
    const n = L.length;
    const target = 0.06 * this.rate;         // aim for ~60 ms in the buffer
    if (!this.playing) {
      if (this.count >= 0.04 * this.rate) this.playing = true;
      else {
        L.fill(0);
        if (R) R.fill(0);
        return true;
      }
    }
    if (this.count > 0.25 * this.rate) {     // far behind (tab was in the background): jump ahead
      const drop = Math.floor(this.count - target);
      this.readPos = (this.readPos + drop) % this.size;
      this.count -= drop;
      this.frac = 0;
    }
    // Play up to 0.5 % faster or slower to keep the buffer near the target without clicks.
    const drift = Math.max(-0.005, Math.min(0.005, (this.count - target) / this.rate * 0.05));
    const step = (this.rate / sampleRate) * (1 + drift);
    for (let i = 0; i < n; i++) {
      if (this.count < 2) {                  // ran dry: output silence until enough has arrived
        this.playing = false;
        for (; i < n; i++) { L[i] = 0; if (R) R[i] = 0; }
        break;
      }
      const a = this.readPos;
      const b = (a + 1) % this.size;
      const f = this.frac;
      const l = this.left[a] + (this.left[b] - this.left[a]) * f;
      const r = this.right[a] + (this.right[b] - this.right[a]) * f;
      if (R) { L[i] = l; R[i] = r; } else { L[i] = (l + r) * 0.5; }
      this.frac += step;
      while (this.frac >= 1 && this.count > 1) {
        this.frac -= 1;
        this.readPos = (this.readPos + 1) % this.size;
        this.count--;
      }
    }
    return true;
  }
}

registerProcessor('homeplay-pcm', HomePlayPcm);
