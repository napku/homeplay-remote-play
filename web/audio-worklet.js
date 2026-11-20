// HomePlay sound player: a small, self-tuning jitter buffer.
//
// It takes raw 16-bit PCM or decoded Opus (float), resamples when the PC's sample rate differs
// from this device's, and keeps only as much sound buffered as the connection needs:
//   - when sound runs dry and the next piece arrives soon after, that was network jitter,
//     so the buffer grows (+20 ms);
//   - when it runs dry because the PC went quiet (the block counter skips), nothing changes;
//   - while the buffer never dips low for a couple of seconds, it shrinks (-5 ms).
// On a steady connection this settles around 25-35 ms instead of a fixed 60 ms.
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
    this.rate = sampleRate;                  // sample rate of the incoming sound
    this.playing = false;
    this.targetSec = 0.05;                   // buffer to aim for; adapts between min and max
    this.minSec = 0.025;
    this.maxSec = 0.15;
    this.dryAt = -1;                         // when we last ran out of sound (seconds)
    this.windowFrames = 0;
    this.windowLow = Infinity;               // lowest buffer level seen while playing (seconds)
    this.windowGrew = false;
    this.port.onmessage = (e) => this.push(e.data);
  }

  write(l, r) {
    if (this.count >= this.size) {           // overflow: forget the oldest frame
      this.readPos = (this.readPos + 1) % this.size;
      this.count--;
    }
    this.left[this.writePos] = l;
    this.right[this.writePos] = r;
    this.writePos = (this.writePos + 1) % this.size;
    this.count++;
  }

  push(m) {
    if (!m) return;
    if (m.rate > 0) this.rate = m.rate;
    if (this.dryAt >= 0) {                   // sound is back after running dry
      // m.gap: the PC skipped blocks on purpose (it went quiet), so running dry was expected.
      // Otherwise, if sound came back quickly, it was late on the network: buffer a bit more.
      if (!m.gap && currentTime - this.dryAt < 0.25) {
        this.targetSec = Math.min(this.maxSec, this.targetSec + 0.02);
        this.windowGrew = true;
      }
      this.dryAt = -1;
    }
    if (m.l) {                               // decoded Opus: one float array per channel
      const L = m.l, R = m.r || m.l;
      for (let i = 0; i < L.length; i++) this.write(L[i], R[i]);
    } else if (m.pcm) {                      // raw sound: 16-bit, interleaved
      const ch = Math.max(1, m.channels | 0), pcm = m.pcm;
      const frames = Math.floor(pcm.length / ch);
      for (let i = 0; i < frames; i++) {
        const l = pcm[i * ch] / 32768;
        this.write(l, ch > 1 ? pcm[i * ch + 1] / 32768 : l);
      }
    }
  }

  adapt(n) {
    this.windowFrames += n;
    if (this.playing) this.windowLow = Math.min(this.windowLow, this.count / this.rate);
    if (this.windowFrames < 2 * sampleRate) return;          // every ~2 s
    if (!this.windowGrew && this.windowLow !== Infinity && this.windowLow > 0.02) {
      this.targetSec = Math.max(this.minSec, this.targetSec - 0.005);
    }
    this.port.postMessage({ bufferMs: Math.round(this.targetSec * 1000) });
    this.windowFrames = 0;
    this.windowLow = Infinity;
    this.windowGrew = false;
  }

  process(inputs, outputs) {
    const out = outputs[0];
    const L = out[0];
    const R = out.length > 1 ? out[1] : null;
    const n = L.length;
    const target = this.targetSec * this.rate;
    this.adapt(n);
    if (!this.playing) {
      if (this.count >= target * 0.8) this.playing = true;
      else {
        L.fill(0);
        if (R) R.fill(0);
        return true;
      }
    }
    if (this.count > Math.max(0.2 * this.rate, target * 3)) {  // far behind (tab was hidden): skip ahead
      const drop = Math.floor(this.count - target);
      this.readPos = (this.readPos + drop) % this.size;
      this.count -= drop;
      this.frac = 0;
    }
    // Play up to 0.5 % faster or slower to keep the buffer near the target without clicks.
    const drift = Math.max(-0.005, Math.min(0.005, (this.count - target) / this.rate * 0.05));
    const step = (this.rate / sampleRate) * (1 + drift);
    for (let i = 0; i < n; i++) {
      if (this.count < 2) {                  // ran dry: silence until enough has arrived
        this.playing = false;
        this.dryAt = currentTime;
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
