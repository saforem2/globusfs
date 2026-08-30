# globusfs

An [fsspec](https://filesystem-spec.readthedocs.io/) filesystem for
[Globus](https://www.globus.org/) collections.

> **Status: working.** Verified against three live collections —
> including **ALCF Eagle** (`alcf#dtn_eagle`, 747 project directories),
> where `ls`, `glob`, `info`, `open()` with mid-file seek, and sparse
> ranged reads all work against production Lustre. Writes (`PUT`/`DELETE`)
> round-trip on Globus Tutorial Collection 1. Anonymous pyarrow column
> projection works on a public collection — one column of sixty, a few KB
> instead of 360 KB — while that collection was intermittently returning
> backend-fault 404s.

```python
import globusfs

# Browser login once; tokens persist to ~/.globusfs/tokens.json
fs = globusfs.filesystem("<collection-uuid>")

fs.ls("/")
with fs.open("data/file.parquet", "rb") as f:
    ...
```

Public collections need no credentials:

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

## Known ALCF collections

Resolved via `endpoint_search`; ALCF's docs list names, not UUIDs.

| Collection | UUID | Type |
|---|---|---|
| `alcf#dtn_eagle` | `05d2c76a-e867-4f67-aa57-76edeb0beda0` | mapped |
| `alcf#dtn_flare` | `f39a7a0f-5bfc-46ce-9615-ba9f8592814f` | mapped |
| `alcf#dtn_grand` | `3caddd4a-bb35-4c3d-9101-d9a0ad7f3a30` | mapped |
| Globus Tutorials on ALCF Eagle | `a6f165fa-aee2-4fe5-95f3-97429c28bf82` | guest, public |

Display names work too — `globusfs.filesystem("alcf#dtn_eagle")` resolves
the name to its UUID before building any scopes. (Passing a name to
`login()` directly requires an authenticated client to resolve it; UUIDs
never need a lookup.)

Eagle's collection root is already `/eagle/projects`, so paths are
project-relative: `fs.ls("/datascience")`, not `/eagle/projects/datascience`.

Expect roughly **2 s per metadata or read operation** through the DTN —
fine for sparse reads and exploration, not for per-record access in a
training loop. See the note on training workloads below.

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

## Pairing with Globus Streaming

[Globus Streaming][streaming] solves a different problem, and the two
compose rather than compete.

|  | `globusfs` | Globus Streaming |
|---|---|---|
| Abstraction | files with byte offsets | TCP socket between two processes |
| Addressing | `globus://<uuid>/path` | `<host>:<port>` contact string |
| Random access | yes — `seek`, ranged reads | no; ordered byte stream |
| Data at rest | reads stored files | no file access at all |
| Mechanism | HTTPS + Transfer API | `LD_PRELOAD` socket interception |
| Platform | any Python 3.10+ | Linux x86_64, Python 3.12+ |

Streaming intercepts only connection *establishment* — `bind()`,
`connect()`, `getaddrinfo` — so unmodified binaries tunnel through
firewalls with no per-byte overhead, but there is no file to open and no
offset to seek to. It cannot back an fsspec filesystem. Conversely
`globusfs` pays an HTTP round-trip per read and cannot see a live feed.

The natural split is **streaming writes it, `globusfs` reads it back**:
an instrument ships frames to a facility during an experiment, and
analysis addresses the resulting files afterward.

**During the experiment** — on the instrument host, pipe the detector
into a socket that Globus tunnels to the facility:

```bash
# One-time: create a tunnel at https://app.globus.org/streams
globus-streams environment initialize --globus-contact dtn.alcf.anl.gov:8888 "$TUNNEL_ID"

# Frames leave the instrument through an ordinary TCP socket.
globus-streams-launch.sh "$TUNNEL_ID" ./detector-stream --host dtn.alcf.anl.gov --port 8888
```

On the facility side a listener accepts the stream and writes it to the
collection's filesystem — plain files on Lustre, nothing Globus-specific:

```bash
globus-streams environment initialize --listener-contact-string 10.0.2.164:8888 "$TUNNEL_ID"
globus-streams-launch.sh "$TUNNEL_ID" ./ingest --out /eagle/myproject/run042/
```

**Afterwards** — the frames are now stored files, so `globusfs`
addresses them from anywhere, no tunnel and no login on the instrument:

```python
import globusfs

fs = globusfs.filesystem("05d2c76a-e867-4f67-aa57-76edeb0beda0")  # alcf#dtn_eagle

frames = fs.glob("/myproject/run042/*.h5")
print(f"{len(frames)} frames landed")

# Read only what you need: headers first, before pulling any bulk data.
for path in frames[:10]:
    with fs.open(path, "rb") as f:
        magic = f.read(8)
        f.seek(fs.info(path)["size"] - 4)
        footer = f.read(4)
    print(path, magic, footer)
```

Why the handoff is clean: streaming's job ends once bytes are on disk,
and that is exactly where `globusfs` starts. Neither knows about the
other.

Two caveats. Streaming authenticates every leg of the route but leaves
end-to-end encryption to your application. And for the analysis pass,
if you are reading *every* byte of *every* frame repeatedly — training
rather than inspection — stage with Globus Transfer instead, per the
note above.

[streaming]: https://docs.globus.org/globus-connect-server/v5/streaming-guide/

## Tests

Characterization tests hit a real public collection and are marked
`network`:

```bash
pytest                    # everything
pytest -m "not network"   # offline only
```

## License

MIT
