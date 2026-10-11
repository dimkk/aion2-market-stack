# Contributing

Thank you for improving AION2 Market Stack. Keep changes small, evidence-bound
and safe to reproduce.

1. Open an issue describing the observable problem or feature contract.
2. Do not attach captures, TLS key logs, credentials, cookies or account data.
3. Add or update tests at the first changed contract boundary.
4. Run `python -B scripts/validate_self_hosted.py` and the affected test suites.
5. Separate implemented, tested and live-verified claims in the pull request.

Third-party code requires an exact upstream URL, immutable ref and license
review. Do not paste code from unrelated protocol projects into this repository
merely because the behavior looks useful.
