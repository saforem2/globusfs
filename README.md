# globusfs

An [fsspec](https://filesystem-spec.readthedocs.io/) filesystem for
[Globus](https://www.globus.org/) collections.

> **Status: working.** Verified end to end against two live collections:
> authenticated `ls`/`info`/`open` on Globus Tutorial Collection 1, and
> anonymous pyarrow column projection on a public collection (one column
> of sixty — a few KB instead of 360 KB) while that collection was
> intermittently returning backend-fault 404s.

```python
import globusfs

# Browser login once; tokens persist to ~/.globusfs/tokens.json
fs = globusfs.filesystem("<collection-uuid>")

fs.ls("/")
with fs.open("data/file.parquet", "rb") as f:
    ...
```

Public collections need no credentials — and no `globus-sdk`:

```python
import fsspec, pyarrow.parquet as pq

fs = fsspec.filesystem(
    "globus",
    collection_id="isaac",
    https_url="https://g-05a4b6.2d513.8443.data.globus.org",
)

with fs.open("isaac/ability/ALL_2007-01.parquet", "rb") as f:
    # Reads only the bytes this column needs, over HTTP range requests.
    table = pq.ParquetFile(f).read(columns=["author"])
```

## Why

Globus is how large scientific datasets actually move between facilities,
but there is no fsspec backend for it — so pyarrow, pandas, dask, and
grain can't read a Globus collection the way they read `s3://` or
`gs://`. This fills that gap with one backend that serves all of them.

## How it works

Globus Connect Server exposes two services, and this needs both:

| Concern | Service | Why |
|---|---|---|
| Reading bytes | HTTPS collection endpoint | Serves full HTTP range semantics: 206, `Content-Range`, mid-file seeks, multipart |
| Listing / metadata | Transfer API | The HTTPS interface has no directory listings |

The read path subclasses `fsspec`'s `HTTPFileSystem`, which already
speaks exactly the range dialect GCS serves.

### Server quirks this works around

Verified against a live collection:

- **Backend flakiness surfaces as a `404`.** GCS load-balances across
  GridFTP backends; a failing one returns `ENDPOINT_ERROR` / `GCS Manager
  Internal Error` rendered as HTTP 404 — byte-identical in status to a
  genuinely missing file, and sticky for the life of a connection.
  Observed failure rates on the public test collection swung from 0/20 to
  20/20 within minutes, hitting files, directories, and the collection
  root alike. Retries need a *fresh* connection, and the only way to tell
  a transient error from a real miss is to parse the body. **A client
  that treats 404 as "absent" will report healthy data as missing.**
- **Suffix ranges (`bytes=-8`) return `416`**, which is how parquet
  readers typically seek to the footer. Because `info()` knows the true
  size, readers can use absolute offsets instead.
- **`HEAD` is unusable.** A HEAD 404 carries no body — and the body is
  the *only* thing distinguishing a backend fault from a real miss. So
  HEAD results are permanently ambiguous. Size and existence come from
  the Transfer API, or from a ranged `GET` (which does return a body and
  carries the total in `Content-Range`).
- **The HTTPS interface has no directory listings at all** — hence the
  Transfer API for metadata.

## Credentials

Token acquisition is pluggable, because it varies more than anything
else: a public collection needs nothing, a portal already has a token,
an interactive user needs a browser.

| Provider | Use |
|---|---|
| `AnonymousCredentials` | Public collections (default) |
| `StaticToken` | A token you already hold |
| `CallableToken` | Fetch on demand — the pickle-safe option |
| `AppCredentials` | Wraps `globus_sdk` `UserApp`/`ClientApp` |

Two constraints worth knowing, both from the globus-sdk docs:

- **`GlobusApp` is not thread-safe**, but fsspec shares one filesystem
  across threads. `AppCredentials` serializes every call through a lock.
- **fsspec pickles filesystems to worker processes.** A live token in the
  constructor args would be copied into every worker payload, so
  `StaticToken` refuses to pickle; use `CallableToken` reading from the
  environment or shared storage so workers re-read rather than receive.

Also set `request_refresh_tokens=True` — it defaults to `False`, and
without it a long run dies when the access token expires mid-epoch.

## A note on training workloads

This is built for *remote and sparse* reads: column projection,
exploration, data not yet staged. For distributed training, staging with
Globus Transfer to node-local scratch beats per-record HTTPS on every
axis — no per-record latency, no token expiry mid-epoch, and it works
with formats like ArrayRecord whose readers do their own seeking.

## Tests

Characterization tests hit a real public collection and are marked
`network`:

```bash
pytest                    # everything
pytest -m "not network"   # offline only
```

## License

MIT
