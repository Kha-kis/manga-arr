# Vendored frontend dependencies

This directory holds the third-party JS that the app templates load via
`<script src="/static/...">`. The files are **vendored** (checked into
the repo) rather than downloaded at container build time — that way:

  - The build is reproducible offline (no curl, no CDN dependency).
  - The runtime image doesn't need `curl` / `libcurl4t64` / `libnghttp2-14`
    just for a one-shot fetch (Trivy was flagging CVE-2026-27135 in
    libnghttp2 as a result of curl's transitive deps).
  - Bumping a version is a deliberate, reviewable diff with a SHA256
    that matches upstream — no silent CDN tampering window.

## Pinned versions

| File             | Library    | Version | Source                                                    | SHA256                                                              |
|------------------|------------|---------|-----------------------------------------------------------|---------------------------------------------------------------------|
| `htmx.min.js`    | HTMX       | 2.0.4   | <https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js>        | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `alpine.min.js`  | Alpine.js  | 3.14.3  | <https://unpkg.com/alpinejs@3.14.3/dist/cdn.min.js>        | `689f513978d11d69f4d33794f7296c9a586a2e55de79bb447cddbc3f474f9f07` |

## Updating a vendored file

```bash
# Replace VERSION + URL + DEST as needed.
DEST=app/static/htmx.min.js
URL='https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js'

curl -sL -o "$DEST" "$URL"
sha256sum "$DEST"   # paste the new hash into the table above
```

A regression test (`tests/python/test_static_assets_provenance.py`) verifies
that the on-disk SHA matches the table above — the test fails noisily if
the files are ever bumped without updating the provenance, or vice
versa.

## Licenses

Both libraries are MIT-licensed. License text is included in the minified
files' header comments.
