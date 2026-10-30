# Third-party notices

This plugin's own code is under the MIT licence (`LICENSE`). It includes one third-party file and relies on
software that is installed separately.

## Included: Silero VAD model

`assets/silero_vad.onnx` is the Silero VAD v6 model (`silero_vad_v6.onnx` as shipped with faster-whisper 1.2.1),
from https://github.com/snakers4/silero-vad.
SHA-256: `4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2`

```
MIT License

Copyright (c) 2020-present Silero Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Not included: installed separately

None of these are part of this repository; Hermes or Docker installs them from their own sources, under their
own licences.

| Software | Licence | How it is used |
|---|---|---|
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | MIT | the host this plugin runs in |
| [piper-tts](https://github.com/OHF-Voice/piper1-gpl) (with espeak-ng) | GPL-3.0-or-later | the voice; imported at runtime, installed by Hermes into its environment |
| Piper voices, e.g. `cs_CZ-jirka-medium` | per voice, see each voice's model card | downloaded by Hermes on first use |
| [onnxruntime](https://github.com/microsoft/onnxruntime) | MIT | runs the Silero model |
| [NumPy](https://numpy.org) | BSD-3-Clause | audio processing |
| [Asterisk](https://www.asterisk.org) in the [andrius/asterisk](https://github.com/andrius/asterisk) image | Asterisk: GPL-2.0; image build files: MIT | separate container, pulled by `install-asterisk.sh`, talks to the plugin over AudioSocket |

Piper is GPL-3.0-or-later. If you distribute this plugin together with Piper (for example in a bundled
runtime or image), the combined distribution has to follow the GPL.
