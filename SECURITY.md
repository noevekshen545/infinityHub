# Security policy

[English](README.en.md) · [Русский](README.md)

## Supported versions

The `main` branch is the supported version. Prebuilt portable archives are supported
for the version they were built from.

## Reporting a vulnerability

Please use **GitHub → Security → Report a vulnerability** (private advisory) instead of
a public issue.

Include:

- affected version or commit;
- operating system;
- steps to reproduce;
- expected and actual behavior;
- logs or screenshots without private data;
- impact assessment.

Do not include real tokens, passwords, private notes or personal file paths in the
report. Replace them with `REDACTED`.

## Scope

In scope:

- the local HTTP server, routes and WebSocket endpoints;
- profile and password handling;
- LAN access policy;
- extension installation, permissions and sandbox;
- file manager, sharing, encryption and path handling;
- archive extraction and `.inf` packages;
- update and recovery mechanisms.

Out of scope:

- vulnerabilities in third-party components (Hermes, llama.cpp, ffmpeg, VLC, Calibre,
  ComfyUI, Electron, Python packages) — report them upstream;
- models and prompt behavior;
- social engineering;
- issues that require physical access to an unlocked machine;
- denial of service from a user with local administrator rights.

## Security model

- The server listens on `127.0.0.1` by default.
- LAN access is a separate switch and requires a password.
- Profiles isolate data; sharing is explicit.
- Extensions request permissions; sandbox restricts files, network, subprocesses and
  execution time.
- Secrets should be stored in local configuration or environment variables, never in
  Git.

## Safe harbor

Good-faith research that follows this policy is welcome. Do not access, modify or
publish other people's data, and do not run destructive tests against systems you do
not own.
