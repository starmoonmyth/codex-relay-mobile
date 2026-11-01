# Third-party notices

The MIT license at the repository root applies to original project code. Third-party components remain under their respective licenses. License texts are retained in `source/licenses/` and bundled into the desktop applications.

## Browser libraries

| Component | License | Source |
| --- | --- | --- |
| marked | MIT | https://github.com/markedjs/marked |
| DOMPurify | Apache-2.0 OR MPL-2.0 | https://github.com/cure53/DOMPurify |

Exact browser package versions, source tarball URLs, and integrity values are recorded in `source/vendor.json`.

## Python runtime and libraries

Desktop builds include Python and installed dependencies such as Pillow, qrcode, pywebpush, py-vapid, cryptography, requests, aiohttp, and their dependencies. Their license and notice texts are in `source/licenses/`. Original third-party copyright notices remain intact.

PyInstaller is used to package the application. Its GPL license includes an exception allowing distribution of applications built with its bootloader; see `source/licenses/pyinstaller-COPYING.txt` for the complete terms.

Some dependencies, including pywebpush and certifi, are distributed under MPL-2.0. Their unmodified sources are available from their upstream projects and versioned PyPI source distributions:

- https://github.com/web-push-libs/pywebpush
- https://pypi.org/project/pywebpush/2.5.0/#files
- https://github.com/certifi/python-certifi
- https://pypi.org/project/certifi/#files

Release build dependency versions are recorded alongside the release artifacts. OpenAI Codex and Tailscale are external prerequisites and are not redistributed here.
