# Security policy

Mynah is a local, single-user application. It listens only on loopback by
default and has no authentication layer. If you start it with `--host 0.0.0.0`,
anyone who can reach the port may access projects, voice recordings, generated
audio, and the machine's inference hardware.

Do not expose Mynah directly to the public internet. Put authentication and TLS
in front of it if you deliberately operate it beyond a trusted local network.

To report a vulnerability privately, use GitHub's **Report a vulnerability**
flow on the repository Security page. If that option is unavailable, open an
issue requesting a private contact channel without including sensitive details.
Please include affected versions, reproduction steps, and practical impact.
