# Windows and WSL release validation — 2026-09-08

Both platforms passed against public commit
[`09ae217`](https://github.com/seena18/mynah/commit/09ae217eb025895fd26d0c40bb46feeb14daed47).
No application changes were needed during this retest.

## Environment

The same RTX 4080 SUPER (16 GB), NVIDIA driver 591.86, was tested sequentially:

| | Native Windows | WSL2 |
|---|---|---|
| OS | Windows 11 Pro, build 26200 | Ubuntu 22.04, kernel 6.6.87.2-microsoft-standard-WSL2 |
| Python | 3.12.10 | 3.12.14 |
| PyTorch | 2.6.0+cu124 | 2.6.0+cu124 |
| CUDA runtime | 12.4 | 12.4 |
| Device selected by mynah | `cuda` | `cuda` |
| Unit and browser suite | 44 passed, 14.206 s | 44 passed, 12.436 s |

Each platform used a fresh public clone and a new virtual environment installed
with `uv sync --locked`. The platform-specific dependency sources selected CUDA
without a manual torch reinstall.

Each server started with its own empty Hugging Face cache and fetched the
2.99 GB model without a login. Both fetched Chatterbox Turbo revision
`749d1c1a46eb10492095d68fbcf55691ccf137cd`. Existing model caches and projects
were not used. Dependency downloads could use uv's existing package cache;
this was not a benchmark of completely uncached dependency installation.

## Functional results

All of these passed on both platforms with real CUDA inference:

- Upload and compile a 9.3-second synthetic speech reference.
- Split an English script and generate both lines.
- Edit a rendered line and generate it with Ctrl+Enter.
- Re-roll unchanged text and receive different audio at the same take URL.
- Verify the untouched line's audio hash remains identical after both operations.
- Decode and play the stitched preview, then download a valid 24 kHz WAV.
- Reload the project and retain its text and takes.
- Switch projects and clear the previous project's preview.
- Stop an active, longer generation; verify a stopped event and an empty queue.
- Return to the original project and verify its untouched audio again.

| Observation | Native Windows | WSL2 |
|---|---|---|
| Per-line rendering, server log | 0.4–0.7 s | 0.4–0.5 s |
| Stop click to idle, observed in UI | 0.71 s | 0.72 s |
| Export duration | 4.83 s | 4.73 s |
| Export sample rate | 24,000 Hz | 24,000 Hz |
| Browser JavaScript errors | 0 | 0 |

These are short test lines, not a general throughput benchmark. UI completion
also includes state polling; rendering timings above come from the server log.
Generation is stochastic, so exported durations differ between runs.

The 44-test suite ran Chromium locally on each tested platform with synthetic
inference. The real-model UI checks drove Chromium on macOS through an SSH
localhost tunnel to each server. The reference was uploaded as a file; physical
microphone capture, voice likeness, and long-form narration quality were not
evaluated in this retest.
