# Third-Party Notices

Interview Copilot is licensed under the Apache License, Version 2.0. The project license does not replace the licenses of third-party software.

## Scope and dependency boundary

This inventory covers third-party Python packages directly imported by the Python files currently tracked in this repository, together with components that those packages are documented to include or statically link. Python standard-library modules, local `copilot` modules, other transitive dependencies, external API services, and software that is not directly imported or bundled are outside this inventory.

Python dependency declarations are in `pyproject.toml`; there is no resolved lock file. Before distributing a bundled application, wheel set, container, or executable, regenerate notices from the exact resolved artifacts and preserve any additional license or notice files shipped with them.

## Direct imports

| Dependency | Use and status in the current source | Upstream license | License source |
| --- | --- | --- | --- |
| [FastAPI](https://github.com/fastapi/fastapi) | Core HTTP API and static/SSE responses; imported unconditionally by `server.py`. | MIT | [Upstream `LICENSE`](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| [HTTPX](https://github.com/encode/httpx) | Core HTTP client used by STT, LLM, enrichment, paper, material, and related test code. | BSD-3-Clause | [Upstream `LICENSE.md`](https://github.com/encode/httpx/blob/master/LICENSE.md) |
| [NumPy](https://github.com/numpy/numpy) | Cross-platform audio sample processing, imported by the legacy audio engine. | BSD-3-Clause | [Upstream `LICENSE.txt`](https://github.com/numpy/numpy/blob/main/LICENSE.txt) |
| [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | Windows WASAPI audio capture. Its Windows extension retains PyAudio code and statically links its bundled PortAudio build. | Mixed: Apache-2.0 (PyAudioWPatch changes); MIT (retained PyAudio code); MIT (bundled PortAudio code) | [PyAudioWPatch `LICENSE.txt`](https://github.com/s0d3s/PyAudioWPatch/blob/8b68e78c31dd1e12e9c603901b8cd0d9d6510b2a/LICENSE.txt); retained [PyAudio notice](https://github.com/s0d3s/PyAudioWPatch/blob/8b68e78c31dd1e12e9c603901b8cd0d9d6510b2a/setup.py#L1-L27); bundled [PortAudio `LICENSE.txt`](https://github.com/s0d3s/PyAudioWPatch/blob/8b68e78c31dd1e12e9c603901b8cd0d9d6510b2a/portaudio_v19/LICENSE.txt) |
| [python-sounddevice](https://github.com/spatialaudio/python-sounddevice) | macOS CoreAudio input capture through PortAudio. Installed only on macOS. | MIT; PortAudio is MIT | [Upstream `LICENSE`](https://github.com/spatialaudio/python-sounddevice/blob/master/LICENSE); [PortAudio `LICENSE.txt`](https://github.com/PortAudio/portaudio/blob/master/LICENSE.txt) |
| [pypdf](https://github.com/py-pdf/pypdf) | PDF text extraction. Feature-specific dependency, but currently imported unconditionally by `copilot/resume.py`, which `server.py` imports at startup. | BSD-3-Clause | [Upstream `LICENSE`](https://github.com/py-pdf/pypdf/blob/main/LICENSE) |
| [Uvicorn](https://github.com/Kludex/uvicorn) | ASGI server runner imported only when `server.py` is executed as the main program; an alternative ASGI runner may be used instead. | BSD-3-Clause | [Upstream `LICENSE.md`](https://github.com/Kludex/uvicorn/blob/master/LICENSE.md) |
| [markdown-it-py](https://github.com/executablebooks/markdown-it-py) | Server-side answer Markdown rendering. | MIT | [Upstream `LICENSE`](https://github.com/executablebooks/markdown-it-py/blob/master/LICENSE) |
| [qrcode](https://github.com/lincolnloop/python-qrcode) | SVG pairing QR generation; no raster image dependency required. | BSD-3-Clause | [Upstream `LICENSE`](https://github.com/lincolnloop/python-qrcode/blob/main/LICENSE) |
| [Lucide](https://lucide.dev/) | Vendored 0.468.0 UMD browser icons in `static/vendor/lucide.js`. | ISC; retained Feather portions MIT | Complete supplied notice: [`static/vendor/LICENSE`](static/vendor/LICENSE) |

The licenses identified above are permissive (Apache-2.0, MIT, or BSD-3-Clause). They may be distributed alongside this Apache-2.0-licensed project, but each dependency and bundled component remains governed by its own terms and its notices must be retained. This is a source inventory, not legal advice.

## License texts and notices

The following notices reproduce the upstream license files and retained source notices linked below. The complete Apache License, Version 2.0 text is also provided in this repository's `LICENSE` file.

### FastAPI — MIT

```text
The MIT License (MIT)

Copyright (c) 2018 Sebastián Ramírez

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

### HTTPX — BSD-3-Clause

```text
Copyright © 2019, [Encode OSS Ltd](https://www.encode.io/).
All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

* Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### NumPy — BSD-3-Clause

```text
Copyright (c) 2005-2025, NumPy Developers.
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

    * Redistributions of source code must retain the above copyright
       notice, this list of conditions and the following disclaimer.

    * Redistributions in binary form must reproduce the above
       copyright notice, this list of conditions and the following
       disclaimer in the documentation and/or other materials provided
       with the distribution.

    * Neither the name of the NumPy Developers nor the names of any
       contributors may be used to endorse or promote products derived
       from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### PyAudioWPatch — Apache-2.0 changes with retained MIT components

PyAudioWPatch's project-level notice is Apache-2.0, but that is not the package's complete licensing boundary. At upstream revision [`8b68e78c31dd1e12e9c603901b8cd0d9d6510b2a`](https://github.com/s0d3s/PyAudioWPatch/tree/8b68e78c31dd1e12e9c603901b8cd0d9d6510b2a), its Python and C binding sources retain the PyAudio MIT notice, and its Windows build configuration statically links the bundled `portaudio_v19` library. The three notices follow.

#### PyAudioWPatch project notice — Apache-2.0

Source: [PyAudioWPatch `LICENSE.txt`](https://github.com/s0d3s/PyAudioWPatch/blob/8b68e78c31dd1e12e9c603901b8cd0d9d6510b2a/LICENSE.txt)

```text
Copyright (c) 2022 S0D3S

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

#### Retained PyAudio notice — MIT

Source: the retained notice in PyAudioWPatch [`setup.py`](https://github.com/s0d3s/PyAudioWPatch/blob/8b68e78c31dd1e12e9c603901b8cd0d9d6510b2a/setup.py#L1-L27), also present in [`src/pyaudiowpatch/__init__.py`](https://github.com/s0d3s/PyAudioWPatch/blob/8b68e78c31dd1e12e9c603901b8cd0d9d6510b2a/src/pyaudiowpatch/__init__.py#L1-L22) and [`src/_portaudiomodule.c`](https://github.com/s0d3s/PyAudioWPatch/blob/8b68e78c31dd1e12e9c603901b8cd0d9d6510b2a/src/_portaudiomodule.c#L1-L25).

```text
PyAudio is distributed under the MIT License:

Copyright (c) 2006 Hubert Pham

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

#### Bundled PortAudio notice — MIT

Source: PyAudioWPatch's bundled [`portaudio_v19/LICENSE.txt`](https://github.com/s0d3s/PyAudioWPatch/blob/8b68e78c31dd1e12e9c603901b8cd0d9d6510b2a/portaudio_v19/LICENSE.txt). PyAudioWPatch's [`setup.py`](https://github.com/s0d3s/PyAudioWPatch/blob/8b68e78c31dd1e12e9c603901b8cd0d9d6510b2a/setup.py#L84-L115) documents static linking for its Windows extension.

```text
Portable header file to contain:
>>>>>
/*
 * PortAudio Portable Real-Time Audio Library
 * PortAudio API Header File
 * Latest version available at: http://www.portaudio.com
 *
 * Copyright (c) 1999-2006 Ross Bencina and Phil Burk
 *
 * Permission is hereby granted, free of charge, to any person obtaining
 * a copy of this software and associated documentation files
 * (the "Software"), to deal in the Software without restriction,
 * including without limitation the rights to use, copy, modify, merge,
 * publish, distribute, sublicense, and/or sell copies of the Software,
 * and to permit persons to whom the Software is furnished to do so,
 * subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be
 * included in all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 * EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 * MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
 * IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR
 * ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
 * CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
 * WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 */

/*
 * The text above constitutes the entire PortAudio license; however,
 * the PortAudio community also makes the following non-binding requests:
 *
 * Any person wishing to distribute modifications to the Software is
 * requested to send the modifications to the original developer so that
 * they can be incorporated into the canonical version. It is also
 * requested that these non-binding requests be included along with the
 * license above.
 */
<<<<<


Implementation files to contain:
>>>>>
/*
 * PortAudio Portable Real-Time Audio Library
 * Latest version at: http://www.portaudio.com
 * <platform> Implementation
 * Copyright (c) 1999-2000 <author(s)>
 *
 * Permission is hereby granted, free of charge, to any person obtaining
 * a copy of this software and associated documentation files
 * (the "Software"), to deal in the Software without restriction,
 * including without limitation the rights to use, copy, modify, merge,
 * publish, distribute, sublicense, and/or sell copies of the Software,
 * and to permit persons to whom the Software is furnished to do so,
 * subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be
 * included in all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 * EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 * MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
 * IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR
 * ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
 * CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
 * WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 */

/*
 * The text above constitutes the entire PortAudio license; however,
 * the PortAudio community also makes the following non-binding requests:
 *
 * Any person wishing to distribute modifications to the Software is
 * requested to send the modifications to the original developer so that
 * they can be incorporated into the canonical version. It is also
 * requested that these non-binding requests be included along with the
 * license above.
 */
<<<<<
```

### python-sounddevice — MIT

```text
MIT License

Copyright (c) 2015-2025 Matthias Geier

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

python-sounddevice uses PortAudio. The complete PortAudio MIT notice is reproduced above under “Bundled PortAudio notice — MIT”; it also applies when PortAudio is supplied dynamically or with a platform wheel.

### pypdf — BSD-3-Clause

```text
Copyright (c) 2006-2008, Mathieu Fenniak
Some contributions copyright (c) 2007, Ashish Kulkarni <kulkarni.ashish@gmail.com>
Some contributions copyright (c) 2014, Steve Witham <switham_github@mac-guyver.com>

All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

* Redistributions of source code must retain the above copyright notice,
this list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above copyright notice,
this list of conditions and the following disclaimer in the documentation
and/or other materials provided with the distribution.
* The name of the author may not be used to endorse or promote products
derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```

### Uvicorn — BSD-3-Clause

```text
Copyright © 2017-present, [Encode OSS Ltd](https://www.encode.io/).
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the copyright holder nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
