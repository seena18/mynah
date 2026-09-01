"""mynah — self-hosted voice cloning TTS.

The environment defaults below are set here, at package import, so they are in
place whether the server is started through run.py or directly with uvicorn.
Each is read by a library at its own import or call time; setdefault leaves
any value the user chose alone.
"""

import os

# tokenizers warns about fork-after-parallelism on every generation otherwise.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# A self-hosted tool should not phone home about which model it loaded.
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
# The vocoder prints a progress bar per chunk; in a server log that is noise.
os.environ.setdefault("TQDM_DISABLE", "1")
