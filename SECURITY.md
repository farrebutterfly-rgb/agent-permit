# Security

Report a vulnerability through GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability"). Do not open a public issue for it.

You get an acknowledgement within three days and a fix or a workaround within fourteen days for anything that affects the default configuration. The advisory is published when the fix is released.

Supported: the latest release on PyPI or the main branch. Older versions get no fixes.

What runs on every push and nightly: CodeQL (security-and-quality queries), pip-audit against the installed dependency set, and a CycloneDX SBOM as a build artifact. Dependabot keeps dependencies and actions current.
