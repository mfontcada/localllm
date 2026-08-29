class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.position = 0;
    this.ratio = sampleRate / 16000;
  }

  process(inputs) {
    const samples = inputs[0]?.[0];
    if (!samples?.length) return true;

    const values = [];
    for (let position = this.position; position < samples.length; position += this.ratio) {
      const index = Math.min(samples.length - 1, Math.round(position));
      values.push(Math.max(-1, Math.min(1, samples[index])));
      this.position = position + this.ratio;
    }
    this.position -= samples.length;

    if (values.length) {
      const buffer = new ArrayBuffer(values.length * 2);
      const view = new DataView(buffer);
      values.forEach((value, index) => {
        view.setInt16(index * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
      });
      this.port.postMessage(buffer, [buffer]);
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);
