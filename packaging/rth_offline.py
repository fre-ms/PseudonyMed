"""Runtime hook of the packaged program, run before anything else: ONNX Runtime (RapidOCR's
engine) reads ``ORT_DISABLE_TELEMETRY`` once, when it is loaded, and must find it set;
docrepro's tools set it as well, this makes it hold even if something loaded the library
earlier."""

import os

os.environ["ORT_DISABLE_TELEMETRY"] = "1"
