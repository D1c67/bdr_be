# RFP Ingestion: Sandbox and Sanitization

Design record and operations guide for the first slice of RFP Ingestion: the
Ingestion Sandbox (an out-of-process, credential-free PDF processor) and the
Ingestion Sanitization pipeline around it. Since 0125 the intake also takes
Word and Excel files (section 2.1): they are converted to PDF outside this
process and the PDF goes through everything below. Later slices (the agent that reads
the outputs and drafts a bid) build on the outputs defined here; nothing in
this slice creates projects, sends email, or touches the bidding pipeline.

v2 folds in a four-lens adversarial review of v1 (security, correctness,
operability, codebase fit). The canonical contract lives in code:
`app/sandbox/protocol.py` (child <-> parent protocol, reject codes, version).
Keep this file in sync with it.

Naming, used everywhere: setting `rfp_ingest_enabled` (canonical env
`RFP_INGESTION_ENABLED`; the older `RFP_INGEST_ENABLED` spelling is still
accepted as an alias and is what `tests/conftest.py` pins), gate
`require_rfp_ingest`, `GET /features` key `rfp_ingest`, router prefix
`/rfp-ingest`, queue job type `rfp_ingest`, page `/ingestion-sandbox`, FE
namespace `ingestionSandbox`, nav key `nav.ingestionSandbox`.

That one setting is the master switch for BOTH RFP Ingestion slices. Besides
this sandbox it arms the email intake (`docs/RFP_EMAIL_INGESTION.md`,
`GET /features` key `rfp_email_ingest`) whenever
`RFP_EMAIL_INGESTION_INBOXES_ALLOWED` is non-empty, which starts a Graph
polling loop against real mailboxes. Keep that var empty until the email slice
is wanted.

---

## 1. Threat model and the trust boundary

Inbound RFP files are attacker-controlled bytes. The realistic threats, in the
order they actually bite:

1. Resource bombs: a tiny PDF that declares a 20,000 x 20,000 point page,
   carries thousands of pages, or embeds a JBIG2/JPX/Flate stream that takes
   minutes or gigabytes to decode. Without bounds one email OOM-kills the API
   container (2 uvicorn workers, no memory limit recorded).
2. Parser exploits: a crafted file that exploits the PDF parser itself. The
   parser is the attack surface, not the file extension.
3. Active content: JavaScript, OpenAction/AA, Launch, embedded files, URI
   links, XFA, remote GoTo. Rasterizing to images neutralizes all of these.
4. Prompt injection (visible or hidden instructions aimed at the model that
   will later read these pages). Out of scope for this slice; the boundary is
   drawn so the agent slice can treat every output as untrusted content, and
   the text contract records hidden-character counts per page.

Two trust zones:

- The API process (trusted, holds the service-role key and Graph credentials).
  It fetches bytes, writes them to quarantine, spawns the sandbox, validates
  what comes back with byte-level checks only, and uploads derived outputs.
  It never parses a PDF and never decodes a JPEG.
- The sandbox child (untrusted once it has opened the file). It has a scrubbed
  environment, no credentials, no app code beyond `app/sandbox/`, hard
  rlimits (soft == hard) on memory, CPU, file size, open files and process
  count, a wall-clock kill from the parent, its own uid (a per-slot uid from a
  pool when the parent is root), and a 0700 output directory nobody else can
  touch. It reads one file from local disk and writes images, text and a
  progress log to one directory. It never touches the network or Supabase.

Everything the child produces is re-validated by the parent as untrusted:
names are derived, never read from the log; files are opened relative to a
directory fd with O_NOFOLLOW and must be regular, single-link, owned by the
sandbox uid and under a byte cap before they are read; JPEG structure is
checked by a hand-written marker parser (no decoder); the decode itself
happens in a second, fresh sandbox invocation (`--verify`).

Honest limitations:

- A subprocess on Railway cannot be denied network access. The compensating
  control is that the child holds nothing worth stealing (no credentials, no
  tokens, no `.env`, a cwd outside the repo) and its outputs are re-validated.
  Moving the same CLI into a `--network none` container later is a deployment
  change, not a rewrite; `protocol.py` is the only interface.
- A grandchild that calls `setsid()` escapes `killpg`. `RLIMIT_NPROC=1` on a
  dedicated uid prevents the fork in the first place; on Linux the parent also
  sets `PR_SET_CHILD_SUBREAPER` so escaped descendants re-parent to it and
  can be reaped. A PID namespace closes this fully (later).
- Local development is NOT a security boundary: the parent is not root, so no
  uid switch happens and the child runs as the developer. Never run
  exploit-class samples locally while `.active-db` is prod; use
  `docker run --network none --user 65534` for those.
- The existing `GET /emails/{id}/attachments/{id}/download` route still serves
  raw attachments regardless of the sandbox verdict. It is an unverified
  surface; surfacing the verdict there is a later slice.

---

## 2. Definition of "verified"

A file is verified when the raw bytes are never needed again. Downstream
consumers use only the derived outputs, which our own pipeline produced under
bounds and then re-checked. The raw file stays in the quarantine bucket for
audit and re-runs only; nothing ever issues a signed URL for it.

Per-file status (`rfp_ingest_files.status`), verdict code in `reject_code`:

| status | meaning |
|---|---|
| `pending` | quarantined (or waiting to be materialized), not yet processed |
| `running` | claimed by a runner (`claim_token` set) |
| `verified` | every check passed, every page rendered |
| `verified_with_gaps` | passed, some pages failed under the gap threshold; placeholders recorded |
| `rejected` | permanent verdict; `reject_code` in `protocol.REJECT_CODES` |
| `failed` | retryable infrastructure outcome; `reject_code` in `protocol.FAIL_CODES` |

Checks, all recorded in `rfp_ingest_files.manifest` (jsonb):

- Identity: sha256, byte size (measured after materialization, never copied
  from Graph's encoded `size`; for an office file both describe the RAW
  file, not the PDF made from it), original filename (display only,
  sanitized), declared MIME (recorded, never trusted), `source_format`
  (section 2.1), source pointer `{email_id, attachment_id,
  graph_attachment_id, mailbox}` or `upload`.
- Type sniff (parent, `rfp_sanitize.sniff_bytes` for the upload request and
  `sniff_file` for the materialized scratch file, pure bytes, identical
  answers): an empty file is `rejected/empty`; `%PDF-` must occur within the
  first 1024 bytes, else `rejected/not_pdf`; only THEN is a foreign magic AT
  OFFSET 0 (after an optional UTF-8 BOM and leading whitespace for the text
  formats) `rejected/polyglot`: `PK\x03\x04`, `MZ`, `\x7fELF`, `GIF8`,
  `\xff\xd8\xff`, `\x89PNG`, `{\rtf`, `%!PS`, and case-insensitive
  `<!doctype`, `<html`, `<script`, `<?xml`. The order matters: a bare EXE,
  ZIP or HTML page with no PDF header is `not_pdf`, and `polyglot` is
  reserved for files that really carry a PDF behind another format's magic
  (which is what its user-facing sentence says). A file with NO header may
  instead be an office document (section 2.1): with the declared filename as
  an input, `PK\x03\x04` at offset 0 under a `.docx`/`.xlsx` name or the
  OLE2 magic `D0 CF 11 E0 A1 B1 1A E1` at offset 0 under a `.doc`/`.xls`
  name passes with a `source_format` of that extension (the zip is never
  opened, the compound file never walked); the PDF rules are untouched by
  the name, so a `.docx` name over PDF bytes is a PDF and a `PK` file named
  `.pdf` is `not_pdf`. Everything else is a FLAG
  in the manifest, never a verdict: `pdf_header_offset`, `markers_in_head`
  (any of the above found before the header offset, or in the first 1024
  bytes when there is no header), `bytes_after_last_eof` (0 when only
  CR/LF/tab/space follow the last `%%EOF`), `zip_eocd_in_tail` (`PK\x05\x06`
  in the last 64 KB), `missing_eof`, and the `byte_markers` counts for
  `protocol.BYTE_MARKERS` over the whole file. Flags and markers are
  recorded for rejected files too.
- Structure (child): opens in PDFium; password required = `rejected/encrypted`;
  owner-only restrictions are recorded (`owner_restricted`,
  `security_handler_revision`) and allowed; page count in (0, max]; each
  page's long side within `max_page_side_pt`.
- Hazard inventory (child): document JavaScript actions, attachments, XFA
  packets, form type; per page: link actions by type (URI, Launch,
  RemoteGoTo, EmbeddedGoTo), page open/close actions, file-attachment
  annotations. Hazards never reject; they are audit flags.
- Resource behavior: rlimits (recorded as applied or not), per-page stall
  timeout, open-phase timeout, per-file wall clock scaled by page count, disk
  quota mirrored by the parent. Every bound hit is recorded by name in
  `bounds_hit`.
- Output integrity (parent, byte level): expected names derived from the page
  index; every artifact opened with `dir_fd` + `O_NOFOLLOW`, `fstat` must show
  a regular file, `st_nlink == 1`, `st_uid == sandbox uid` when a switch is in
  force, `st_size` under the tier cap; sha256 and size match the log; JPEG
  marker walk: SOI, only `APP0/DQT/SOF0/SOF2/DHT/DRI/SOS/RSTn/EOI` segments,
  SOF dimensions equal the logged (w, h), `max(w, h)` within the tier, EOI is
  the last byte. The APP0 must be the FIRST segment after SOI and must be the
  exact 16-byte JFIF segment a Pillow encode writes (identifier `JFIF\0`,
  version 1.00 to 1.02, density unit at most 2, both densities non-zero, no
  embedded thumbnail); a second APP0 is invalid. That leaves no room in the
  file for attacker-chosen bytes, which an unconstrained APP0 (up to 64 KB
  each, ignored by the `--verify` decode) would have carried straight into
  `rfp-derived`. Text files read with a bounded read, UTF-8 (`errors=
  "replace"`), sanitized (see below), sha256 checked, and refused when the
  re-sanitized text is longer than `max_text_chars_per_page` or longer than
  the `chars` the log itself claimed; a `width_pt` or `height_pt` of 0 is
  refused at the boundary. The out dir is walked with `lstat` and refused if
  it holds anything unnamed or any non-regular entry (stderr and progress
  logs excepted, and those must have `st_nlink == 1`); the progress log must
  be a regular single-link file owned by the sandbox uid, and the stderr tail
  is read only from a regular single-link file owned by the parent, which
  created it and never chowns it. The log is parsed incrementally with a
  64 KB line cap, `parse_constant` refusing NaN/Infinity, `RecursionError`
  caught, numbers `isfinite` and range-checked, `page_count` validated before
  any per-page structure exists. Any mismatch = `failed/invalid_output`, and
  nothing is uploaded.
- Image delivery (parent, two bounded reads, never an accumulation): the
  parent must not hold the whole file's derived images. The validation above
  reads each artifact once into a bounded buffer, hashes it, walks its markers
  and then DROPS the buffer, keeping only `{w, h, bytes, sha256}`. Afterwards,
  and only for a file that is going to be `complete` (so after the `--verify`
  pass and the gap rule), every ok page is re-opened in index order through
  the same `dir_fd` + `O_NOFOLLOW` + `fstat` discipline, read once more into a
  bounded buffer and re-hashed against the digest recorded at validation, then
  handed to the caller's page sink one page at a time. A mismatch anywhere is
  `failed/invalid_output` for the whole file and no further page is delivered,
  so the window between the two reads is closed (the child is dead by then and
  the out dir is the parent's own). Peak parent image memory is one page plus
  one upload batch, not the file, and the batch is itself bounded in bytes
  (`_UPLOAD_BATCH_BYTES`, 96 MB) as well as in pages (`_UPLOAD_BATCH_PAGES`,
  32), so a file of very large pages flushes early instead of holding 32 of
  them. Page text also stays on the result, but it too is bounded across the
  file: the runner spends `max_text_bytes_per_file` in index order, and once
  it is gone every later page comes back with `text == ""` (`text_chars`
  still reports the true count) and the file records
  `bounds_hit: text_bytes`. The same cap is applied a second time when
  `text.json` is written, and `max_text_chars_per_page` still bounds any
  single page.
- Decode verification (second sandbox invocation, `--verify`): a fresh child
  under the same limits and uid decodes every JPEG fully and reports
  `(file, w, h, ok)`; a page whose image does not decode is marked
  `failed/verify` (counts as a gap).
- Provenance: sandbox version, protocol version, pypdfium2/PDFium (with V8
  and XFA build flags when exposed) / Pillow / Python versions, platform, uid
  and gid used, the exact limits document, `limits_applied`, `limits_hash`,
  timestamps, elapsed, restarts, peak RSS, stderr tail (sanitized, 4 KB).

Text contract: per page, control characters (category Cc) other than `\n`
and `\t` are stripped without being counted (`\r` included, so PDFium's
CRLF line endings become LF; U+0000 dropped), and code points in categories
Cf (`format_chars`), Co (`private_use`), Cn and Cs (both `unassigned`, there
is no separate surrogate key) plus U+FFFD (`replacement_chars`) are stripped
with their counts recorded as `text_hazards`, so the agent slice can treat
pages with hidden content as suspicious. Nothing is normalized. The child
(`app/sandbox/textclean.py`) and the parent (`rfp_sanitize.sanitize_text`)
implement the same table and a test compares them code point for code point;
the parent keeps `max(child, parent)` per counter. Only the sanitized text is
stored.

Gaps: a failed page (page size, render error, memory, crash, stall, verify,
aborted) gets a placeholder. The file stays usable if failed pages are at
most `max(RFP_INGEST_MIN_FAILED_PAGES_ALLOWED, ceil(ratio x page_count))`
(defaults 2 and 5 percent); above that it is `rejected/too_many_failed_pages`.
A file with no ok page at all is rejected the same way even when its failure
count is inside that floor, because the floor exists to let a mostly rendered
file through and a file with nothing rendered gives a reviewer nothing to
look at.

Idempotency: within a run, a duplicate sha256 is refused at upload (409) and
recorded as `rejected/duplicate` for email attachments (the partial unique
index on `(run_id, sha256)` is the check; the CAS update that sets sha256
catches the 23505). `sha256`, `sandbox_version`, `protocol_version` and
`limits_hash` are stored on every file row for later cross-run reuse.

### 2.1 Office files

Word and Excel files (`.docx`, `.xlsx`, and the legacy `.doc`, `.xls`) are
accepted on every intake path (the page upload, the email attachment path,
and `add_upload_file` as the platform harvesters call it) while
`rfp_ingest_office_files_enabled` is on; off, they are `rejected/not_pdf`
exactly as before, with no deploy. The boundary is drawn so that nothing
inside this deployment ever parses an office document:

- The parent recognises one by bytes and declared extension together
  (section 2: OOXML magic + `.docx`/`.xlsx`, OLE2 magic + `.doc`/`.xls`),
  records `source_format` on the file row and stores the raw bytes in
  quarantine as `{run_id}/{file_id}/source.<ext>` with the format's content
  type. The extension and the type come from the sniff, never from the
  declared name. Nothing ever mints a signed URL for quarantine.
- Conversion happens in the Gotenberg service that already serves the
  office previews and the RFQ sends (`office_preview.py`, setting
  `gotenberg_url`; local runs `bdr-gotenberg` on :3500, Railway has the
  service). The runner (`rfp_ingest._convert_office`, client
  `rfp_office_convert.py`) streams the quarantined bytes as one multipart
  part named `source.<ext>` to `POST {gotenberg}/forms/libreoffice/convert`
  under `rfp_ingest_office_convert_timeout_seconds` (Gotenberg sends
  nothing until LibreOffice is done, so the read timeout is the conversion
  bound) and writes the answer to the scratch `source.pdf` the child would
  have read, under a running byte cap of `rfp_ingest_max_file_bytes`. No
  spreadsheet option is set (in particular not `singlePageSheets`, which the
  previews use: a one-page sheet the size of a poster would fail the page
  side cap, ordinary pagination renders every page). LibreOffice runs in
  Gotenberg's container, so the parser that touches the attacker-controlled
  document is neither the API process nor the sandbox child.
- Before a `.docx` / `.xlsx` is sent, `rfp_office_convert.external_content_verdict`
  takes a bounded look at its zip container (never at the document as a
  document: the `rfp_zip` helpers check the end record before `zipfile`
  parses the directory, inflate members in chunks under a cap, and parse
  the inspected parts as XML with expat, so an entity-encoded attribute, a
  prefixed element, a UTF-16 part or a field instruction split across runs
  reads the way LibreOffice reads it, never the way a regex over raw bytes
  would). An external relationship in any `.rels` other than a hyperlink
  (an image, an attached template, an OLE object, a frame, an external
  workbook, a package: things LibreOffice may fetch while importing; a URL
  or UNC target counts as external whatever TargetMode says), an embedded
  object (`embeddings/`, `oleObject*`), an `xl/externalLinks/` part, a
  workbook data connection or query table (`xl/connections.xml`,
  `xl/queryTables/`), or a DDE / INCLUDE field instruction in any XML part
  under `word/` refuses the conversion: `rejected/conversion_rejected`
  with `conversion.reason = "external_content"` and the finding under
  `conversion.scan` (`external_rel:<type>`, `embedding`, `external_link`,
  `data_connection`, `dde`, `include_field`, `bad_xml` when an inspected
  part is not well-formed XML or declares a DTD, `member_too_large` (one
  part over its cap, or the inspected parts over 128 MB together),
  `too_many_members`, `central_directory_too_large`, or `bad_zip` when the
  container cannot be opened and so was not scanned). The original stays
  in quarantine. The
  legacy `.doc` / `.xls` binaries cannot be scanned this way and still go
  as they are, which is why the converter must run without network egress
  (section 6).
- The returned PDF is attacker-influenced bytes and is treated as an
  upload: the parent runs the same byte sniff over it (PDF rules, no
  filename; a non-PDF answer is `rejected/conversion_rejected`), the child
  renders and verifies it exactly like an uploaded PDF (pages, text, images
  PDF, hazards, the works), and the manifest records the step as
  `conversion: {engine: "gotenberg", route, duration_ms, http_status,
  pdf_sha256, pdf_bytes, source_format, reused, pdf_sniff, converted_path}`.
  The derived PDF is uploaded to the derived bucket as
  `{run_id}/{file_id}/converted.pdf` (never signed by `file_urls`); the
  per-file re-run cleanups (`/retry`, the stale-running reset, a cancel, a
  verdict after a delivery) spare that one object, and a re-run reuses it
  when its digest matches the earlier manifest's `conversion.pdf_sha256`
  (`reused: true`), otherwise it converts again. `delete_run` and the
  retention prune remove the whole run prefix, and the prune clears
  `converted_path` with the other path columns.
- Outcomes: converter unreachable, timed out or 5xx is
  `failed/conversion_unavailable`, a retryable FAIL code the run's retry and
  stale-claim machinery picks up like a storage failure; 4xx (an unsupported
  or corrupt document), an empty answer or a non-PDF answer is
  `rejected/conversion_rejected` ("The file could not be converted to
  PDF."); an answer over the cap is `rejected/too_large`. A failed attempt
  still records its `conversion` block (with `reason`) in the manifest.
  Identity (sha256, size, the duplicate index) is always the raw file's,
  and the page budget is checked before any conversion is attempted.

---

## 3. Components

```
app/sandbox/                     THE SANDBOX (child). Imports ONLY stdlib,
  __init__.py                    pypdfium2 and Pillow. Never app.core or
  protocol.py                    app.services. A test spawns a fresh
  limits.py                      interpreter and asserts sys.modules.
  hazards.py                     document + page hazard inventory (PDFium raw)
  render.py                      page render (tiers) + text extraction
  textclean.py                   text sanitizer + hazard counters (stdlib)
  __main__.py                    CLI: process mode and --verify mode

app/services/rfp_sanitize.py     parent-side sniff, byte markers, sha256,
                                 string/text sanitizers, JPEG marker walk
app/services/rfp_sandbox_runner.py  spawn/monitor/kill/respawn the child,
                                 uid slots, progress parsing, output validation
app/services/rfp_image_pdf.py    streaming DCTDecode writer for images-NNN.pdf
app/services/rfp_ingest_storage.py  the two buckets, path builders, streaming
                                 download, upload with retry, recursive delete,
                                 bucket-keyed signed-url memo
app/services/rfp_ingest.py       run orchestration, queue job, marks, bell,
                                 retention prune, self-test
app/services/rfp_office_convert.py  office -> PDF through Gotenberg (2.1)
app/routers/rfp_ingest.py        dev-only API (prefix /rfp-ingest)
supabase/migrations/0119_rfp_ingestion_sandbox.sql
supabase/migrations/0121_rfp_ingest_indexes.sql
supabase/migrations/0125_rfp_ingest_office_files.sql
bdr_fe/app/(app)/ingestion-sandbox/page.tsx  dev-only page
```

### 3.1 Sandbox launch contract

```
<sys.executable> -I -X utf8 -c <protocol.BOOTSTRAP> <repo_root> \
    --spawn <n> --input <pdf> --out <dir> --limits <json> [--skip-file <path>]
<sys.executable> -I -X utf8 -c <protocol.BOOTSTRAP> <repo_root> \
    --verify --out <dir> --limits <json> --list-file <path>
```

`--spawn <n>` is the 0-based spawn index (the child writes
`progress.NN.jsonl` with O_EXCL; the verify line carries no `--spawn`, the
parent picks a fresh NN for its stderr file). `<json>` is the PATH of the
limits document the parent wrote under its work dir (`limits.NN.json`); the
child also accepts the document inline when the argument starts with `{`.

- `cwd` = the out dir. Environment = `{"LANG": "C.UTF-8", "TMPDIR": <out>/tmp,
  "HOME": <out>/tmp}` and nothing else. `stdin=DEVNULL`, `stdout=DEVNULL`,
  `stderr=` an `O_EXCL` file `<out>/stderr.NN.log` (never a pipe: an undrained
  pipe deadlocks the child at 64 KB and reads as a stall). `close_fds=True`
  (default), `start_new_session=True`.
- uid: `rfp_ingest_sandbox_uid` defaults to 65534. When the parent's euid is 0
  the runner claims a SLOT uid from `rfp_ingest_sandbox_uid_pool_base ..
  +pool_size-1` with `fcntl.flock` on `<scratch_root>/rfp-ingest-slot-N.lock`
  (blocking with lease renewal every 30 s while waiting), spawns with
  `user=slot_uid, group=slot_uid, extra_groups=[]`, and records `uid`, `gid`,
  `slot`, `uid_switch_applied=True`. A value of 0 disables the switch even as
  root (explicit opt-out, logged loudly at boot). When the parent is not root
  the switch is skipped and recorded as not applied. The pool defaults to
  base 60100, size 4 (2 workers x concurrency 1 needs 2; headroom for 2).
- Scratch layout per file: `mkdtemp` under the scratch root, then
  `chmod 0o711`; `source.pdf` written by the parent with mode 0o644;
  `<out>` created by the parent, `chown(uid, uid)`, `chmod 0o700`;
  `<out>/tmp` likewise. The parent (root) can read 0700 dirs it does not own.
  Locally (no switch) 0o700 owned by the developer.
- The child applies rlimits FIRST, each as `(v, v)` in its own
  `try/except (OSError, ValueError)`, records `limits_applied` from a
  `getrlimit` readback, writes `start`, THEN imports pypdfium2 and Pillow and
  writes `ready` (versions, PDFium V8/XFA flags when `pypdfium2.version`
  exposes them; a V8 build exits `EXIT_BAD_ARGS`). `RLIMIT_CORE=0`,
  `RLIMIT_NPROC=1`, `RLIMIT_NOFILE=64` are constants in the limits document
  (Settings does not expose them). `RLIMIT_FSIZE` = `max_output_file_bytes`
  (default 64 MB), NOT the disk quota. On macOS `RLIMIT_AS` raises ValueError
  and is recorded as not applied; CPU, FSIZE, NOFILE, NPROC, CORE apply.
- `--skip-file`: newline-separated 0-based page indices to skip (an index at
  or over `max_pages`, or a non-integer, is `EXIT_BAD_ARGS`). The parent lists
  the blamed indices AND every index that already has a `page` event, so a
  respawn never re-renders pages the parent already holds; the child does not
  care and overwrites whatever it does process. On a respawn the limits
  document is rewritten with `deadline_seconds` = the remaining file budget
  (min 5 s); `limits_hash` ignores that key.
- The child creates `<out>/thumb`, `<out>/full` and `<out>/text` itself
  (0700, exist_ok) before `start`; the parent creates `<out>` and `<out>/tmp`.
- Exit code 0 whenever the child wrote an `end` or `reject` event, and after
  verify mode; `EXIT_BAD_ARGS` (2) with one app-authored stderr line for an
  argument or limits problem, a missing `--input`, an existing progress or
  verify file, an import failure of pypdfium2/Pillow, or a V8 PDFium build
  (which writes `ready` first, for provenance); anything else is a crash. The
  parent never trusts exit codes alone.

Output directory layout:

```
<out>/progress.NN.jsonl     one per spawn, append-only, one os.write per event
<out>/stderr.NN.log         one per spawn
<out>/verify.jsonl          written by the --verify invocation
<out>/thumb/NNNN.jpg        classification tier
<out>/full/NNNN.jpg         reading tier (page-size aware, see 3.3)
<out>/text/NNNN.txt         sanitized text, UTF-8
<out>/tmp/                  the child's TMPDIR/HOME
```

### 3.2 Progress protocol

Every line is one JSON object with an `event` key, written with a single
`os.write` of the full line under `O_APPEND`. Order within a spawn:

1. `start`: `sandbox_version`, `protocol_version`, `spawn`, `pid`, `uid`,
   `gid`, `limits` (echo), `limits_applied` (`memory, cpu, fsize, nofile,
   nproc, core` booleans plus the readback values), `skip_count`.
2. `ready`: `versions` (python, pypdfium2, pdfium, pillow), `pdfium_flags`
   (`{v8, xfa}` or null), `platform`.
3. `heartbeat` (`{phase, elapsed_ms}`): the single-threaded child does not
   run a timer; it writes four bracketing heartbeats (before and after the
   open call, before and after the inventory) so the log shows which phase a
   kill landed in. Ignored by validation; the parent's open timeout is the
   real bound. `heartbeat_seconds` stays in the limits document for a later
   timer-driven child.
4. `document`: `page_count`, `pdf_version`, `owner_restricted`,
   `security_handler_revision`, `form_type`, `metadata` (capped strings),
   `hazards` (document level).
   OR `reject`: `code` in `CHILD_REJECT_CODES`, `detail` (capped).
5. Per page, in index order, skipping the skip list: `page_start` `{index}`
   emitted BEFORE any PDFium call for that page, then `page` with
   `status: "ok"`: `index, width_pt, height_pt, rotation, tier
   ("full"|"full_small"), thumb {file, w, h, bytes, sha256}, full {file, w, h,
   bytes, sha256}, text {file, chars, truncated, sha256, hazards}, hazards
   (page level), render_ms` or `status: "failed"`: `index, code, detail`.
6. `end`: `pages_ok`, `pages_failed`, `elapsed_ms`, `peak_rss_kb`
   (kilobytes on every platform), `output_bytes` (thumb + full + text bytes
   written by THIS spawn; the progress log and earlier spawns are not
   counted, the parent's `lstat` mirror is the whole-directory check),
   `aborted` (null or `disk_quota|deadline`; checked before each page, so the
   page that crossed the quota still has its event).

Parent monitor loop (every 0.5 s while the child runs): poll the process;
tail the current progress file and parse only COMPLETE lines (a line without
`\n` is left for the next tick); count progress only when a complete valid
event was parsed; sum `lstat` sizes over `<out>` and `killpg` on
`> max_output_total_bytes` (`bounds_hit: disk_quota`); enforce the open
timeout (no `document`/`reject` within `RFP_INGEST_OPEN_TIMEOUT_SECONDS` of
`ready`, heartbeats do not extend it), the page stall (no complete event for
`RFP_INGEST_PAGE_STALL_SECONDS` after the first `page_start`) and the file
wall clock; call `should_abort()` (cancel, shutdown, lost lease) and kill on
True; update the file row's `phase`, `pages_done`, `last_progress_at`,
`child_pid` every 10 s (direct update fenced on `claim_token`); renew the
queue lease every 30 s.

Kill discipline on EVERY exit path: `os.killpg(pgid, SIGKILL)` while the
Popen object is still unreaped (swallow `ProcessLookupError`), then
`proc.wait()`, then re-read the log to EOF, then attribute blame, then
validate. The post-exit drain loops until end of file rather than reading one
window: a tick's byte window can end mid line and return no lines at all
while the log still has data, so "loop while it returns lines" would truncate
the very tail that carries the terminal event. Never `communicate()`. Each artifact is read into a bounded buffer
and hashed and checked there, and the buffer is dropped; the bytes reach the
uploader through the second bounded read described in section 2 (image
delivery), so nothing accumulates across pages.

Crash attribution is specified in `protocol.py`'s module docstring (it is the
authority), including the child-side rule that the terminal event (`end`, or
a `reject`) is written BEFORE any further PDFium work such as closing the
document. What follows is a summary of that table and must not disagree with
it. Summary: no `start` or `EXIT_BAD_ARGS` -> `failed/spawn` (a parent or
image problem, never the file's fault: no respawn, and the run carries on
with the next file; only a parent-side `SandboxSpawnError` aborts the run,
with `run.error` = `VERDICT_MESSAGES["spawn"]`, app-authored, per 3.6. The
sanitized stderr tail goes in the file's
`manifest.provenance.stderr_tail`, never in an error column);
`start` without `ready` -> `failed/spawn`, as is no `ready` within
`RFP_INGEST_OPEN_TIMEOUT_SECONDS` of the spawn (`bounds_hit: open_timeout`);
`ready` without `document`/`reject` -> `rejected/unreadable` (or
`failed/resource_limit` when the parent killed it for the open timeout); a dangling `page_start` blames
that page (`crash`, or `stall`/`memory` when the parent killed it or
`limits_applied.memory` is true and the exit signal is SIGKILL/SIGABRT after
a memory-sized page); otherwise the first index not covered is blamed;
respawn only when the skip list grew, else `rejected/crash_loop`; before a
respawn the parent unlinks the blamed index's partial artifacts (derived
names, `unlinkat` with `dir_fd`). Restart cap = `max(RFP_INGEST_MAX_CHILD_RESTARTS,
ceil(page_count x ratio))`. A torn trailing line is discarded when the child
did not exit cleanly; every terminated line must parse strictly. A later
spawn's `document` must repeat the pinned `page_count`; a different value or
a `reject` on a respawn is `rejected/unreadable`. `end.aborted` set ->
`failed/resource_limit`; pages without an event are recorded
`failed/aborted`. The `stderr_tail` in the result is a labeled join of two
tails: the last PROCESS spawn that wrote anything, and the verify spawn's own
(`"spawn 2:\n...\nverify:\n..."`, the whole thing trimmed to
`MAX_STDERR_TAIL_BYTES`). Both are kept because the verify child always writes
its `sandbox verify: limits applied ...` preamble, so taking only the
highest-numbered spawn that wrote anything would make that preamble the tail
of every file and hide the traceback of a process spawn that crashed. The `--verify` spawn is never respawned:
pages without an ok line after a verify crash or stall are `failed/verify`,
verify `EXIT_BAD_ARGS` is `failed/spawn`, a malformed verify log is
`failed/invalid_output`. The verify spawn is monitored against a budget of its
own, `max(what is left of the file budget, 2 x RFP_INGEST_PAGE_STALL_SECONDS)`,
because `file_timeout_seconds` is sized off `max_pages_per_file` rather than
the real page count and a long render can leave verify nothing. A wall-clock
kill during verify records `bounds_hit: file_timeout` and fails only the pages
verify did not confirm (the gap rule then decides), instead of throwing away a
fully rendered file as `failed/resource_limit`; cancel, an invalid verify log
and the disk quota stay whole-file verdicts.

Two further parent rules, both of which fail one file rather than the run: a
child that plants a parent-derived name (a spawn's stderr file) inside `<out>`,
which it can write to, is `failed/invalid_output` for that file and not a spawn
error; and a child that has written its terminal event but has not exited
within the page stall is `SIGKILL`ed as `linger`, the verdict it already wrote
stands, and the kill is attributed to no page.

### 3.3 Rendering tiers

- thumb: long side `RFP_INGEST_THUMB_LONG_SIDE` (1568, the splitter's size),
  JPEG quality 70. Rendered as its own PDFium pass (faster than downscaling).
- full: pages whose long side is at most `full_small_threshold_pt` (1300 pt,
  letter/tabloid) render at `RFP_INGEST_FULL_SMALL_LONG_SIDE` (2200 px, about
  200 dpi); larger sheets at `RFP_INGEST_FULL_LONG_SIDE` (4000 px). Quality
  85. The `tier` used is recorded per page. This keeps a 2000-page spec book
  near 1.4 GB instead of 5 GB.
- Rendering uses `draw_annots=True`, no form environment (`init_forms` is
  never called), `may_draw_forms=False`.
- text: `get_textpage().get_text_range()`, capped at
  `max_text_chars_per_page`, sanitized in the child (`textclean.py`) and
  re-sanitized in the parent.

### 3.4 Parent runner (`rfp_sandbox_runner.py`)

```python
@dataclass(frozen=True)
class SandboxLimits: ...   # every protocol.LIMIT_KEYS field; from_settings(s); to_json(); limits_hash()

@dataclass
class PageOutput:
    index: int; status: str; code: str | None; detail: str | None
    width_pt: float | None; height_pt: float | None; rotation: int | None; tier: str | None
    thumb: bytes | None; full: bytes | None      # None whenever a page_sink is given
    thumb_meta: dict | None; full_meta: dict | None   # {w, h, bytes, sha256}
    text: str | None; text_chars: int; text_truncated: bool; text_hazards: dict
    hazards: dict; render_ms: int | None

@dataclass
class SandboxResult:
    status: str        # complete | rejected | failed
    code: str | None   # reject code, or FAIL_* for failed
    detail: str | None
    start: dict | None; ready: dict | None; document: dict | None; end: dict | None
    pages: list[PageOutput]          # every index 0..page_count-1 exactly once
    hazards: dict; bounds_hit: list[str]; restarts: int; elapsed_ms: int
    uid: int | None; gid: int | None; slot: int | None
    stderr_tail: str; spawns: int; peak_rss_kb: int | None

class LeaseLost(Exception): ...
class SandboxSpawnError(Exception): ...   # parent-side: scratch/spawn failure

def run_sandbox(pdf_path: Path, *, limits: SandboxLimits, scratch_root: Path,
                open_timeout_seconds: int, page_stall_seconds: int,
                file_timeout_seconds: int, max_restarts: int,
                max_pages_remaining: int, uid_slot: UidSlot | None,
                should_abort: Callable[[], bool],
                on_progress: Callable[[str, int, int | None], None],
                renew: Callable[[], bool],
                repo_root: Path | None = None, python_executable: str | None = None,
                restart_ratio: float = 0.05, min_failed_pages_allowed: int = 2,
                failed_page_ratio: float = 0.05,
                page_sink: Callable[[PageOutput, bytes, bytes], None] | None = None
                ) -> SandboxResult
```

`run_sandbox` creates its OWN `mkdtemp` work dir under `scratch_root` (a hard
link, or a copy, of `pdf_path` as `source.pdf`; the caller keeps its own
materialization directory and removes it with `cleanup_scratch`) and removes
it before returning. `pages` holds every index exactly once only for a
`complete` result; `rejected/too_many_failed_pages` returns the full list with
the image bytes dropped, `failed/invalid_output` returns `[]`, and every other
rejected or failed verdict returns only the failed placeholders known so far.
Callers upload from a `complete` result only. The gap ratios are passed in by
the caller from Settings. `run_sandbox` never raises for anything the child did; it raises
`SandboxSpawnError` for parent-side faults (cannot create scratch, cannot
spawn) and `LeaseLost` when `renew()` returns False (after killing the
child). On the `document` event, if `page_count > max_pages_remaining` the
child is killed and the result is `rejected/run_page_budget` (no restart
counted). After the process spawn(s) complete, the `--verify` spawn runs over
the list of ok pages; pages it fails are `failed/verify`. Output validation
runs before the result is returned and follows section 2 exactly.

`page_sink` is how the caller takes the image bytes without the parent ever
holding more than one page of them, and it is what production uses. With a
sink, validation drops each buffer once it has been hashed and walked, so
`PageOutput.thumb` and `.full` stay None on every page (`thumb_meta` and
`full_meta` still carry the size and digest that were checked); then, only
when the result is going to be `complete`, the runner re-opens and re-hashes
every ok page in index order per section 2 and calls
`page_sink(page, thumb_bytes, full_bytes)`, keeping no reference to the bytes
after the call. A hash mismatch makes the whole file `failed/invalid_output`
with no further sink calls. An exception raised by the sink propagates out of
`run_sandbox` unchanged (the work dir is still removed in the runner's
`finally`, and the child is long dead by then), which is how the service turns
an upload failure into `failed/storage` and a cancel or a lost lease into its
normal abort path. Without a sink (the smoke script and the fake-child tests)
the validated bytes ride out on the `PageOutput`s of a `complete` result, as
before.

Parent-side JPEG marker walk (`rfp_sanitize.jpeg_dimensions(buf) -> (w, h)`):
SOI at 0; iterate segments; allow only APP0, DQT, SOF0, SOF2, DHT, DRI, SOS;
read dimensions from the SOF; after SOS scan entropy data for RSTn/EOI; EOI
must be the final two bytes; any other marker, truncated segment, or trailing
bytes -> invalid. The APP0 must be the first segment and must be the 16-byte
JFIF segment a Pillow encode writes (identifier, version 1.00 to 1.02, a known
density unit, non-zero densities, no embedded thumbnail); a second APP0 is
invalid. `app/sandbox/render.py` is the only producer of these bytes, so a
change there to a non-Pillow encoder, or a save that adds an ICC profile, EXIF
or a comment, is refused by the parent (a test pins the real Pillow fixture's
APP0 so such a change fails loudly). This is the only "parsing" the parent
does.

`acquire_uid_slot(scratch_root, ..., wait_timeout_seconds=SLOT_WAIT_SECONDS)`
waits a bounded 300 s for a free uid slot and then raises `SlotWaitTimeout`
(logging the pool occupancy) rather than parking a worker forever on a wedged
or leaked slot. The service maps it to a transient fault, so the queue
requeues the run instead of burning the file.

### 3.5 Storage (`rfp_ingest_storage.py`)

Two new private buckets (migration 0119, `public=false`, no storage
policies, explicit `file_size_limit`):

- `rfp-quarantine` (limit 314572800): `{run_id}/{file_id}/source.<ext>`,
  `<ext>` being the row's `source_format` (`pdf`, or one of `docx`, `xlsx`,
  `doc`, `xls` per 2.1; `quarantine_path(run_id, file_id, source_format)`
  refuses anything else). Written once, read only by the runner
  (streaming). NO signed-url function exists for this bucket.
- `rfp-derived` (limit 209715200): `{run_id}/{file_id}/thumb/NNNN.jpg`,
  `{run_id}/{file_id}/full/NNNN.jpg`, `{run_id}/{file_id}/text.json`,
  `{run_id}/{file_id}/images-NNN.pdf`, `{run_id}/{file_id}/manifest.json`,
  and for an office file `{run_id}/{file_id}/converted.pdf`
  (`converted_path`; kept across the per-file re-run cleanups through
  `delete_prefix(..., keep=("converted.pdf",))`, never signed).
  Signed URLs only here, memoized in this module keyed by
  `(bucket, path)` with the same 60 s refresh margin; `download=` for the
  PDF/JSON objects. `storage.py`'s cache is never reused.

Helpers: `upload_bytes(bucket, path, data, content_type)` and
`upload_file(bucket, path, file_path, content_type)` with `upsert=True`
(deterministic keys; a retry after a TLS drop must not 400 on "exists") and
three attempts with `2 s x attempt` backoff on `httpx.TransportError`;
`download_to_file(bucket, path, dest, max_bytes)` streaming the Storage REST
endpoint with the service headers on a short-lived `httpx.Client` (1 MB
chunks, running byte cap; `dest` is created `O_EXCL` before the request and
unlinked on every failure; transport errors get the same three attempts;
HTTP 404 raises `RfpStorageNotFound`, over the cap `RfpStorageTooLarge`);
`delete_prefix(bucket, prefix)` RECURSIVE (paths are three and four levels
deep; the two-level walkers in `storage.py` would miss objects; a blank
prefix is refused, depth is capped at 8); `signed_url(bucket, path, *,
download=None)`, memoized per `(bucket, path, download)`. Every path
builder validates `run_id`/`file_id` as single `[A-Za-z0-9_-]` segments and
every helper refuses a bucket outside the two RFP buckets. Page objects
are uploaded through a `ThreadPoolExecutor(max_workers=4)` on the shared
HTTP/1.1 client (measured 0.25 s per object sequentially).

Supabase enforces `min(project global limit, bucket limit)`; the Dashboard
global limit is 450 MB after 0132 (raise it by hand in the Dashboard). Retention: `rfp_ingest_retention_days`
(default 14); `rfp_ingest.prune_expired()` runs in the queue's hourly prune
slot. For each terminal run older than that it CASes the run to `expired`
FIRST and only then deletes the two prefixes: a losing CAS means a `/retry`
won the race and the objects must be left alone, and a delete that fails after
the CAS leaves the run `expired` (which `/retry` refuses) rather than leaving a
retryable run whose bytes are gone. Rows and manifests are kept, but the path
columns are cleared in the same pass (`derived_prefix`, `images_pdf_paths`,
`text_path` on the file rows, `thumb_path`/`full_path` on the pages rows), so
`file_urls`, `list_pages` and `page_urls` stop minting signed URLs for objects
that no longer exist and the page simply hides its download section. The same
pass does two more sweeps: staging runs whose `updated_at` is older than 48 h
are CASed to `canceled` (fenced on `staging` only, so a run started in the same
second is untouched) and then straight on to `expired`, so `/retry` refuses a
run whose bytes are about to go, before both prefixes are deleted at once
rather than waiting out retention; the file rows' `quarantine_path` is cleared
in the same pass, for the same reason the retention sweep clears the derived
paths. The run keeps its `canceled` error sentence and its `completed_at`, and
the bell still fires once. Finally, orphaned scratch DIRECTORIES
(`rfp-ingest-*`, `rfp-sandbox-*`, `rfp-selftest-*`, never the
`rfp-ingest-slot-N.lock` files and never symlinks) whose whole tree has been
untouched for twice the maximum file timeout are removed, counted and logged.
The newest mtime anywhere in the tree is what counts: a work dir root goes
stale the moment the child writes only into `out/`. The walk carries the same
two bounds the per-file disk accounting does, a per-entry budget
(`3 x max_pages_per_file` plus slack) and a depth ceiling (`_MAX_TREE_DEPTH`,
8, mirroring the runner's `_MAX_OUT_DEPTH`), and it streams `os.scandir`
rather than listing it, so a tree left behind with millions of entries cannot
make the prune the thing that runs the worker out of memory. Such a tree is
logged and LEFT IN PLACE (removing it would walk it again just as hard), the
sweep moves on to the next one, and only trees actually removed are counted.


### 3.6 Run orchestration (`rfp_ingest.py`)

Tables (migration 0119; text + CHECK statuses; RLS enable + force, no
policies; `set_updated_at` triggers; header `-- 0119 - RFP Ingestion sandbox.`
with the 0116/0117 branch-skew note; ends with `notify pgrst, 'reload schema';`):

```
rfp_ingest_runs
  id uuid pk, source_kind text check in ('upload','email'),
  email_id uuid references ingested_emails(id) on delete set null,
  status text check in ('staging','pending','running','done','done_with_errors',
                        'failed','canceled','expired') default 'staging',
  error text, file_count int default 0, files_verified int default 0,
  files_gapped int default 0, files_rejected int default 0, files_failed int default 0,
  pages_total int default 0, limits jsonb, limits_hash text, sandbox_version text,
  protocol_version int, created_by uuid references profiles(id) on delete set null,
  started_at, completed_at, created_at, updated_at
  indexes: (status, created_at desc); (created_at desc);
           (completed_at) where status in ('done','done_with_errors','failed','canceled');
           (email_id); (created_by)

rfp_ingest_files
  id uuid pk, run_id uuid not null references rfp_ingest_runs(id) on delete cascade,
  source_attachment_id uuid references ingested_email_attachments(id) on delete set null,
  filename text not null, declared_mime text, size_bytes bigint, sha256 text,
  quarantine_path text, source_format text not null default 'pdf'
    check in ('pdf','docx','xlsx','doc','xls'), converted_path text,     -- 0125
  status text check in ('pending','running','verified',
    'verified_with_gaps','rejected','failed') default 'pending',
  reject_code text, error text, claim_token text, phase text, pages_done int default 0,
  last_progress_at timestamptz, child_pid int, page_count int, pages_ok int, pages_failed int,
  hazards jsonb, manifest jsonb, derived_prefix text, images_pdf_paths jsonb, text_path text,
  restarts int default 0, elapsed_ms bigint, sandbox_version text, protocol_version int,
  limits_hash text, started_at, finished_at, created_at, updated_at
  indexes: (run_id, created_at); (run_id, status, created_at) where status in ('pending','running');
           unique (run_id, sha256) where sha256 is not null; (source_attachment_id)
  comment on column reject_code: no CHECK; app/sandbox/protocol.py is the authority

rfp_ingest_pages
  id uuid pk, file_id uuid not null references rfp_ingest_files(id) on delete cascade,
  page_index int not null, status text check in ('ok','failed'), code text, detail text,
  width_pt numeric, height_pt numeric, rotation int, tier text,
  thumb_path text, full_path text, thumb_w int, thumb_h int, full_w int, full_h int,
  text_chars int, text_truncated boolean default false, text_hazards jsonb,
  render_ms bigint, created_at
  unique (file_id, page_index)

(Migration 0121 made four of the corrections above: `elapsed_ms` and
`render_ms` widened to bigint, because the parent accepts child-reported
millisecond counts far above the int4 ceiling and a 22003 there cost the whole
file; `rfp_ingest_files_active_idx` on `(status)` alone dropped for the
`(run_id, status, created_at)` partial index that the pending-file query can
actually use; and the two runs indexes the listing and the prune walk need.)

llm_jobs: job_type CHECK widened (drop then add) to
  ('boq_extraction','general_material','proposal_lines','bid_split','rfp_ingest')
claim_llm_jobs: replaced with a 4th parameter `job_types text[] default null`
  (`and (job_types is null or job_type = any(job_types))`), same body otherwise
storage.buckets: the two inserts above, on conflict (id) do nothing
```

Queue integration: ONE `llm_jobs` row per RUN (`job_type='rfp_ingest'`,
`target_id=run.id`, payload `{"run_id"}`, priority 200 via
`rfp_ingest_queue_priority`). `llm_queue` changes:

- `_JobSpec` gains `model_label: Callable[[Settings], str]` (default: the
  existing `llm.active_model(feature, s)`); the rfp spec returns `"sandbox"`.
  `_handle_failure` uses it, and its whole body is wrapped so a failure
  inside it still attempts the terminal CAS (`_execute` really never raises).
  `llm_errors.user_message` tolerates a non-LLM label.
- `llm_errors` gains `KIND_INFRASTRUCTURE` (transient, message = `str(exc)`),
  raised as `rfp_ingest.RfpIngestTransient(RuntimeError)`; permanent app
  errors are `RfpIngestPermanent(ValueError)`.
- `worker_loop` claims in two passes per tick: LLM job types with the LLM
  capacity, and `['rfp_ingest']` with `rfp_ingest_sandbox_concurrency -
  running_rfp` (this pass is skipped while `rfp_ingest_enabled` is false, so
  a disabled feature never spawns a child; queued rows wait for the flag or
  an operator cancel). A sandbox run never occupies an LLM slot and never
  waits for a slot inside a claim. The module-level semaphore in `rfp_ingest`
  is a belt-and-braces assertion, not a waiting room. The claim always names
  its `job_types`, so migration 0119's 4-parameter `claim_llm_jobs` must be
  applied BEFORE this code is deployed (against the old function every claim
  404s at PostgREST until it lands; the tick logs and keeps going).
- `_execute` sets a `current_job` contextvar (next to `llm_gate`'s job id)
  before `spec.run(payload)`; `renew_lease(job) -> bool` reuses `_cas_job`
  with `{"lease_expires_at": now + lease}` so the fence includes `attempts`.
  It returns False only when the CAS genuinely loses (stale attempt, not
  running, not ours); a database or transport error while renewing is logged
  and returns True (a hiccup is not a lost lease; the next 30 s renewal
  retries and the sweep is the backstop).
  `requeue_self(job)` CAS-requeues the current job with `error_kind
  'interrupted'` (used on shutdown). In the BackgroundTasks fallback both are
  no-ops returning True.
- The spec's `current_status` maps every terminal run status to `'done'` so
  the AI monitor's `requeue_terminal` refuses; `/rfp-ingest/runs/{id}/retry`
  is the only retry path.
- `_handle_failure` returns early without writes for `LeaseLost`.
- The hourly prune slot calls `rfp_ingest.prune_expired()` in a try/except.

`_mark(run_id, status=..., **fields)` is a CAS, not an unconditional update:
`pending|running|failed|done|done_with_errors` apply only when the current
status is in `('pending','running')` (`.in_`), `staging -> pending` only via
`/start`, `canceled` is sticky (from `staging|pending|running`), `expired`
only from terminal statuses. A terminal transition that wins the CAS sends
the bell `rfp_ingest.finished` to `created_by` (`mirror_email=False`,
metadata `{run_id, status, files_verified, files_gapped, files_rejected,
files_failed}`, message "RFP ingestion run finished: N verified, N with gaps,
N rejected, N failed", "RFP ingestion run failed: <error>" or "RFP ingestion
run canceled: The run was canceled."); the `expired` transition sends no bell. `_mark(status='failed'|'canceled')` also resets that run's
`running` files to `pending` with `reject_code='interrupted'` cleared on the
next claim. `_derive_run_status` is a caller of `_mark`.

`execute(run_id)` (the spec's `run`):

1. CAS `running` (from `pending|running`); return quietly if it lost. Load
   files. Reset `running` files whose `claim_token` is not ours: CAS them to
   `pending` first, then delete their pages rows and derived prefix. Compute
   `consumed = sum(page_count)` over verified/gapped files.
2. For each `pending` file in `created_at` order, inside one try/except that
   NEVER lets a per-file exception escape (map to `failed/<code>`, app-authored
   message from `protocol.VERDICT_MESSAGES`, full exception in the log only):
   renew the lease; stop cleanly if `should_abort()`; CAS the file to
   `running` with a fresh `claim_token` (fence: `status='pending'`); check free
   disk (`shutil.disk_usage(scratch)`; skip as `failed/storage` when free minus
   the estimate is under the 2 GB reserve); materialize into scratch (upload
   source: `download_to_file` from quarantine; email source: stored attachment
   from `project-files`, or `graph_email.graph_stream` `$value` with the byte
   cap, 404/410 -> `rejected/not_stored`, item attachments ->
   `rejected/item_attachment`, null graph id -> `rejected/not_stored`); sniff
   (with the display filename while office files are on, 2.1; an office
   file's raw bytes are renamed to `source.<ext>` so the child's
   `source.pdf` is free for the converter); sha256 of the raw file;
   CAS-update `{sha256, size_bytes, source_format}` fenced on the claim
   (23505 -> `rejected/duplicate`, `error` = the verdict sentence and the
   winning file id in `manifest.identity.duplicate_of`); upload to
   quarantine (email sources only; uploads are already there); check the run
   page budget (`max_pages_per_run - consumed <= 0` ->
   `rejected/run_page_budget` without a spawn; otherwise the remainder is
   `max_pages_remaining`); for an office file, phase `convert`: reuse
   `converted.pdf` or convert through Gotenberg, sniff the PDF, upload it,
   CAS `converted_path` (2.1; `failed/conversion_unavailable`,
   `rejected/conversion_rejected`, `rejected/too_large`); `run_sandbox`
   with a `page_sink` (section 3.4), which receives each validated page's two
   images one at a time, appends the thumb to the streaming `images-NNN.pdf`
   writer (parts capped at `rfp_ingest_images_pdf_part_bytes`, 150 MB; a
   builder failure is `bounds_hit: images_pdf`, never a file failure) and
   queues the two page objects, flushing them to the derived bucket every
   `_UPLOAD_BATCH_PAGES` (32) pages; every flush first refreshes the file's
   `phase`/`last_progress_at` (which also detects a lost claim), consults
   `should_abort()` and renews the queue lease, because the upload phase of a
   large file runs for minutes with no child alive to do either; then
   finalize the images PDF, re-check the claim, renew the lease and upload the
   tail (`text.json`, built after `run_sandbox` returns from the page text on
   the result, then `manifest.json` and the PDF parts); replace the pages rows
   (delete + insert under a fresh claim check, so the rows and the file row's
   counters always come from the same attempt); CAS the file terminal
   (fence `status='running', claim_token`); add its pages to `consumed` when
   verified/gapped. A result that is not `complete`, and a short delivery
   (fewer pages than the ok list, itself `failed/invalid_output`), deletes the
   derived prefix first: the sink may already have uploaded pages before the
   verdict landed. Scratch is removed in a `finally`. An unexpected
   exception maps to `failed/storage` for storage, HTTP and OS errors and to
   `failed/interrupted` for anything else, with two named exceptions: a
   `SandboxSpawnError` marks the file `failed/spawn`, the run `failed`, and
   leaves the remaining files `pending`; a `SlotWaitTimeout` (no uid slot came
   free within 300 s) is capacity rather than a bad file, so it becomes a
   transient fault and the queue requeues the whole run. A missing quarantine object for an upload source is
   `rejected/not_stored` (a retry cannot recover the bytes).
3. Re-check for `pending` files once (a late insert cannot happen because
   uploads require `staging`, but a requeue race can); any that remain with
   no active job are `failed/orphaned`. `_derive_run_status`: `done` when
   every file is terminal and none is `failed` (all-rejected is still
   `done`); `failed` when every file is `failed`; otherwise
   `done_with_errors`. Counters written on the run row.

Only `LeaseLost`, `SHUTTING_DOWN` and a failure to load or CAS the run row
propagate out of the per-file loop; `LeaseLost` is caught at the top of
`execute` and returns after logging (the queue's succeeded CAS then fails
harmlessly). On shutdown (`rfp_ingest.SHUTTING_DOWN`, a `threading.Event` set
by the lifespan teardown before tasks are cancelled) the runner kills the
child, leaves the file `running`, calls `requeue_self`, and returns.

Cancel (`POST /runs/{id}/cancel`): CAS the run to `canceled` from
`staging|pending|running` FIRST (sticky; the mark also hands the run's
`running` files back to `pending`), THEN call `llm_queue.cancel(job)` on the
active job inside a try/except (it only touches queued/zombie jobs). The
order is deliberate: `llm_queue.cancel` marks the domain row failed through
`mark_from_queue`, and with `canceled` already written that mark is fenced
out, so `canceled` stays what the user sees. A running runner sees
`should_abort()`, kills the child, CAS-resets the in-flight file to `pending`
(fenced on its claim; usually the cancel mark already did it), deletes that
file's derived prefix and pages, and returns normally (the job succeeds;
`_mark` never moves the run out of `canceled`).

Retry (`POST /runs/{id}/retry`): allowed on `failed|done_with_errors|canceled`
only when `llm_queue.active_job('rfp_ingest', run_id)` is None (else 409 with
the job state). Order: CAS the run to `pending` FIRST, fenced on the status
just observed (409 if it moved), and only THEN reset files in
`('failed','pending','running')` to `pending` (clear `claim_token`,
`reject_code`, `error` and the output columns, delete their pages rows and
derived prefixes), THEN `enqueue(raise_on_active=True)`. The run CAS comes
first because a losing CAS means a cancel or another retry got there, and the
file reset would otherwise clear a claim and delete a derived prefix that a
live runner still owns. On `JobAlreadyActive` the run is put back with its
previous status AND its `completed_at` and `error`: status alone would leave a
run that reads terminal to every caller but that the retention prune, which
walks `completed_at`, can never reclaim. Enqueueing last means a job
claimed within the next tick always finds the run `pending` (the other order
could claim before the run row moved and no-op in `execute`). Rejected files
are never revisited.

Delete (`DELETE /runs/{id}`): only when status is in `staging|done|
done_with_errors|failed|canceled|expired`, AND only while
`llm_queue.active_job('rfp_ingest', run_id)` is None (409 otherwise: cancel
first and wait for the runner's acknowledgement). That job row IS the
acknowledgement: `llm_queue.cancel` refuses to cancel a running job with a
live lease, and a dead worker's lease expires on its own, so the absence of an
active job is the only durable proof no runner is still writing. Order: both
storage prefixes FIRST, then the rows (cascade), because the rows are what the
retention prune walks to find the prefixes; a storage failure is a 503
(`RfpIngestTransient`) and leaves the run deletable and reclaimable.

Self-test: `rfp_ingest.self_test()` spawns the child (with the configured uid
switch) on an embedded one-page PDF (hand-written bytes in code) and returns
`{ok, detail, at, elapsed_ms, ...}`. `ok` has THREE states: true, false, and
null for "unknown". When the parent is root and the switch is on it uses a
DEDICATED slot index `pool_size` (uid `pool_base + pool_size`, lock file
`rfp-ingest-slot-<pool_size>.lock`) so it never takes a run's slot; a busy
slot proves nothing, so it returns `ok=null` and is NOT cached, which is what
keeps two workers racing at boot from poisoning each other. Size the uid range
as `pool_size + 1` consecutive uids. True and false ARE cached per worker, but
a cached FAILURE is re-run once it is older than 900 s, so a worker cannot
stay poisoned by one bad moment. It runs once from the lifespan when the flag
is on (awaited through `asyncio.wait_for` with a 120 s bound, logged loudly on
failure or overrun, never blocking boot) and on `GET /status?self_test=1`;
a plain `GET /status` spawns NOTHING, so an unthrottled reader can no longer
stack sandbox children through that route: it serves the 30 s cached report
when there is one and otherwise builds a cheap one (free disk plus slot
occupancy) carrying this worker's LAST self-test verdict, or an app-authored
"has not run on this worker yet" placeholder with `ok: null` when there is
none. Every report says which it is in `self_test_fresh`. Exactly one report is built per worker at a time; a caller
that cannot get the builder within 5 s takes the last report (`cached: true`)
or gets a 503 when this worker has never built one. `execute` refuses to mark
a run `running` only for a fresh real failure; an unknown verdict is a
transient fault and the queue retries the run. The lifespan teardown sets `rfp_ingest.SHUTTING_DOWN` before
cancelling any background task, flag or not.

### 3.7 Email source

`POST /runs {"source": "email", "email_id"}` returns 409 when the email has no
`ingested_email_attachments` rows. Otherwise it creates the run (`pending`)
and one file row per attachment row (`status='pending'`, `quarantine_path`
null, `size_bytes` null, `source_attachment_id` set, `filename` sanitized for
display) and dispatches. The runner materializes each as in 3.6. The Graph
stream: `graph_email.graph_stream(method, path, *, timeout=httpx.Timeout(
connect=10, read=60, write=60, pool=30), prefer=None)` is a context manager
sibling of `graph_request` yielding the streaming response with the same
`Authorization` and `Prefer: IdType="ImmutableId"` headers and
`follow_redirects=False`; it calls `raise_for_status()` before yielding, so
ANY non-2xx (3xx included) raises `httpx.HTTPStatusError` and an error body
can never be streamed into a file; `graph_inbox.
download_attachment_to_file(message_id, attachment_id, *, mailbox, max_bytes,
dest)` consumes it with a running byte counter that closes the response and
raises when the cap is exceeded, writing 1 MB chunks to a file opened
`O_EXCL`. `mailbox` always comes from the `ingested_emails` row.

### 3.8 API (`/rfp-ingest`)

Router: `APIRouter(prefix="/rfp-ingest", dependencies=[Depends(require_rfp_ingest)])`
(404 while the flag is off, before auth) and `Depends(require_dev)` in EVERY
endpoint signature. Rate limits in decorators: `rfp_ingest_rate_limit`
(`RateLimitScope.RFP_INGEST`, `rfp_ingest_rate_limit_per_min` default 120,
`RATE_LIMIT_HELP` entry, `docs/ERROR_CODES.md` row) on every read and on
create/cancel/delete. It counts EVERY role `require_dev` admits, like
`llm_monitor_rate_limit`, and is deliberately not narrowed to the internal
roles: a dev account whose role happens to be `estimator` would otherwise be
exempt from the whole budget on these routes; `ai_rate_limit` on start/retry; `upload_rate_limit` on
file uploads. Service errors map through one helper: `RfpIngestPermanent` ->
its `http_status` (400/404/409/413) with the service's sentence,
`RfpIngestDuplicate` -> 409 with " (existing file <id>)" appended,
`RfpIngestTransient` -> 503. The write routes (create, start, cancel, retry)
answer the full run detail (run + files without manifests + queue poll info)
so the page needs no second request; DELETE answers `{id, deleted: true}`. Every
path uuid is validated before PostgREST, and only in its canonical spelling:
`uuid.UUID` also accepts `urn:uuid:`, `uuid:`, `urn:`, braced and hyphen-less
forms that Postgres then rejects with 22P02, which would surface as an
unhandled 500 with its CORS headers stripped, so anything that does not round
trip is a 404 like any other bad id. `q` on `/emails` goes through the
existing `emails._sanitize_query`. Audit rows: `rfp_ingest.create`, `.start`
(`{file_count, rejected}`), `.cancel`, `.retry`, `.delete` only.

| method | path | notes |
|---|---|---|
| GET | /status | limits, versions, platform notes (incl. `self_test_slot` and the answering worker's `pid`), free scratch disk, self-test result, sandbox slot occupancy, `office_files` (enabled, engine, formats, timeout), `cached`, `self_test_fresh`. Cached for 30 s and spawns nothing, serving the worker's last self-test verdict (or an `ok: null` not-run placeholder); `?self_test=1` is the only caller that spawns, building a fresh report with a live self-test |
| GET | /emails | recent inbound `ingested_emails` with attachment counts, `?q=&limit=` |
| POST | /runs | `{source:"upload"}` -> `staging`; `{source:"email", email_id}` -> `pending` + dispatch; 409 on no attachments |
| POST | /runs/{id}/files | multipart, one file per request (a PDF, or an office file per 2.1), 409 unless `staging`, 413 over `rfp_ingest_max_files_per_run`; sniff + sha256 in the request; row inserted BEFORE the object upload (unique index catches the race; 23505 -> 409 human sentence, no orphan); rejected-at-sniff files are recorded (`rejected`, `quarantine_path` null) and returned 201; the row carries `source_format` |
| POST | /runs/{id}/start | CAS `staging -> pending`, 409 when `file_count == 0` or every file is rejected; dispatch (queue, BackgroundTasks fallback) |
| GET | /runs | `?status=&limit=&offset=` -> `{rows,total,offset,limit}` |
| GET | /runs/{id} | run + files (manifest excluded) + `queue` poll info |
| GET | /files/{id} | file + manifest + hazards (incl. `source_format`, `converted_path` and the manifest's `conversion` block; neither quarantine object nor `converted.pdf` is ever signed) |
| GET | /files/{id}/pages | `?offset=&limit=` (max 500); each row carries a server-minted thumb signed URL; an unknown file id answers an empty page, `GET /files/{id}` is the existence check |
| GET | /pages/{id}/urls | `{full}` signed URL for the reading tier |
| GET | /files/{id}/urls | `{images_pdf: [...], text, manifest}` with `download=` |
| POST | /runs/{id}/cancel | see 3.6 |
| POST | /runs/{id}/retry | see 3.6 |
| DELETE | /runs/{id} | see 3.6 |

### 3.9 Frontend (`/ingestion-sandbox`)

Gate: `profile.is_dev && features?.rfp_ingest === true`. Single-file page in
the splitter's shape: tabs New run / Runs, `?run=` and `?file=` view state
behind `Suspense`, 3 s setTimeout-chain poll while the run is
`pending|running`. New run: DropZone (`.pdf,.docx,.xlsx,.doc,.xls` plus
their MIME types, multiple, folders flattened; other names are skipped
with a notice), create the run, one file per POST wrapped in
`withRateLimitRetry`, then
Start; a `beforeunload` warning while uploading (the loop is page-local:
leaving the page aborts the batch, and the UI says so); or pick an email
from the recent list. Run detail: file table with status badge, verdict code,
phase and pages done while running, pages ok/failed, hazards summary,
elapsed, restarts, a format chip (PDF / Word / Excel from `source_format`,
hidden for `not_pdf`/`empty` verdicts where no format was established);
cancel/retry/delete with the state rules above. File detail: the same chip
next to the verdict, verification card (status, code, document info,
versions, limits applied, uid switch, bounds hit), identity (incl. the
format), hazard table, sniff flags, a conversion card for office files
(outcome, engine and route, duration, HTTP status, PDF size and sha256, the
PDF sniff, reused), page grid of
thumbnails (signed URL fetched into a blob URL because the CSP `img-src` is
`'self' blob: data:`), failed pages as placeholders with their code, click
opens the full tier in a Modal, downloads for the PDF parts / text.json /
manifest.json, manifest in a Collapsible + JsonPre.

Implementation notes: the New run tab navigates to `?run=<id>` only after
`POST /start` answered on a clean batch; when some files failed to send or
Start was refused it stays put with the failure list and an "Open run"
button, and a batch that lands zero files deletes the empty staging run. The
tab also carries an on-demand "Sandbox status" card (`GET /status?self_test=1`
for the live self-test, versions, platform notes, scratch disk, slots, caps)
behind a button, never fetched on mount or polled. An `ok=null` self-test is
shown as inconclusive rather than as a failure, and a `cached` report says so. The run detail offers Start for a
staging run that has files; the Runs tab has a status filter, a manual
Refresh and offset-based Load more, no auto-poll. The file detail polls
every 3 s while `pending|running` and loads the page grid once settled.

Wiring: `lib/features.ts` `Features` gains `rfp_ingest: boolean` (false in
`ALL_ENABLED`); Sidebar `NavItem` next to `AI_MONITOR_NAV` with `devOnly:
true, featureFlag: "rfp_ingest"`, added to `BIDDING_NAV` and to the carry-over
array when Bidding is off; NOT added to `subAppForPath`;
`NotificationsBell` `destination()` gains a case for `rfp_ingest.finished`
-> `/ingestion-sandbox?run=${metadata.run_id}` and the metadata type gains
`run_id?`; `aiMonitor.jobType.rfp_ingest` label; all strings under
`ingestionSandbox.*` and `nav.ingestionSandbox` in `locales/en` (the office
file strings of 2.1, `format.*`, `conversionOutcome.*`, `phase.convert`,
the two conversion codes, the New run wording and `file.conversion*`, are
in every catalog); no em dashes.

---

## 4. Settings (`Settings`, env var = upper snake case)

| setting | default | meaning |
|---|---|---|
| `rfp_ingest_enabled` | false | env `RFP_INGESTION_ENABLED` (alias `RFP_INGEST_ENABLED`); 404s every /rfp-ingest route and hides the page; rides along in GET /features as `rfp_ingest`. Master switch for both slices: it also arms the email intake when `RFP_EMAIL_INGESTION_INBOXES_ALLOWED` is non-empty |
| `rfp_ingest_office_files_enabled` | true | section 2.1: accept `.docx`/`.xlsx`/`.doc`/`.xls` on every intake path and convert them through Gotenberg; false = `rejected/not_pdf` as before, no deploy |
| `rfp_ingest_office_convert_timeout_seconds` | 180 | the converter call's read and write timeout (the read timeout is the conversion bound); at least 1 and below `llm_queue_lease_seconds / 2`, since the lease is not renewed while the worker waits on the converter. Gotenberg's own `--api-timeout` must be at least as long |
| `rfp_ingest_max_file_bytes` | 450 MB | per file; also the Graph stream cap and the cap on a converted PDF; must be <= `upload_max_bytes` |
| `rfp_ingest_max_files_per_run` | 200 | enforced at upload (413) and at email run creation |
| `rfp_ingest_max_pages_per_file` | 3000 | over -> `rejected/too_many_pages` |
| `rfp_ingest_max_pages_per_run` | 20000 | files beyond the budget are `rejected/run_page_budget` |
| `rfp_ingest_max_page_side_pt` | 14400 | 200 inches; larger pages fail individually |
| `rfp_ingest_thumb_long_side` | 1568 | classification tier |
| `rfp_ingest_thumb_jpeg_quality` | 70 | |
| `rfp_ingest_full_long_side` | 4000 | reading tier for large sheets |
| `rfp_ingest_full_small_long_side` | 2200 | reading tier for pages up to the threshold |
| `rfp_ingest_full_small_threshold_pt` | 1300 | long side at or below which the small tier applies |
| `rfp_ingest_full_jpeg_quality` | 85 | |
| `rfp_ingest_max_text_chars_per_page` | 200000 | |
| `rfp_ingest_max_text_bytes_per_file` | 64 MB | text.json cap; beyond -> truncated flag + `bounds_hit: text_bytes` |
| `rfp_ingest_images_pdf_part_bytes` | 150 MB | thumb-tier images PDF part cap (<= derived bucket limit) |
| `rfp_ingest_open_timeout_seconds` | 300 | ready -> document/reject |
| `rfp_ingest_page_stall_seconds` | 120 | no complete event after the first page_start |
| `rfp_ingest_file_timeout_base_seconds` | 300 | file wall clock = clamp(base + per_page_ms x pages, 600, max) |
| `rfp_ingest_file_timeout_per_page_ms` | 1500 | |
| `rfp_ingest_file_timeout_max_seconds` | 14400 | |
| `rfp_ingest_sandbox_memory_mb` | 3072 | `RLIMIT_AS`, and it bounds VIRTUAL size, which runs above RSS. Measured child peaks: 1.18 GB RSS for a 3-sheet 30 x 42 in set at the 4000 px tier, 650 to 700 MB for 36 to 40 sheet sets, 340 MB for a 475-page spec book. 1536 MB was below the worst of those, and on a 1 to 5 page set three `memory` gaps already exceed the gap allowance, so a legitimate drawing set came back `rejected/too_many_failed_pages` with every image dropped |
| `rfp_ingest_sandbox_cpu_seconds` | 1800 | `RLIMIT_CPU` per spawn |
| `rfp_ingest_sandbox_output_file_mb` | 64 | `RLIMIT_FSIZE`, largest single artifact |
| `rfp_ingest_sandbox_disk_mb` | 3072 | out-dir quota, mirrored by the parent |
| `rfp_ingest_scratch_reserve_mb` | 2048 | free disk that must remain before a file starts |
| `rfp_ingest_max_child_restarts` | 5 | floor; effective cap = max(this, ceil(pages x ratio)) |
| `rfp_ingest_max_failed_page_ratio` | 0.05 | gap threshold ratio |
| `rfp_ingest_min_failed_pages_allowed` | 2 | gap threshold floor |
| `rfp_ingest_sandbox_concurrency` | 1 | children per uvicorn worker (2 workers in prod = 2x) |
| `rfp_ingest_sandbox_uid` | 65534 | 0 = no switch even as root (opt-out) |
| `rfp_ingest_sandbox_uid_pool_base` | 60100 | per-slot uids when the parent is root |
| `rfp_ingest_sandbox_uid_pool_size` | 4 | must be >= 2 x concurrency; the self-test uses uid `pool_base + pool_size`, so reserve `pool_size + 1` uids |
| `rfp_ingest_scratch_dir` | "" | empty = system temp; Railway: a volume mount once one exists |
| `rfp_ingest_queue_priority` | 200 | never outranks user-facing LLM jobs |
| `rfp_ingest_retention_days` | 14 | derived + quarantine prune |
| `rfp_ingest_rate_limit_per_min` | 120 | read routes |

`_validate_rfp_ingest`: thumb <= full_small <= full; ratio in [0, 1];
memory >= 512 MB; concurrency >= 1; pool size >= 2 x concurrency;
`max_file_bytes <= upload_max_bytes`; `output_file_mb <= disk_mb`;
`page_stall < llm_queue_lease_seconds / 2`; `images_pdf_part_bytes <=
200 MB`; `1 <= office_convert_timeout < llm_queue_lease_seconds / 2`;
operator-facing messages naming the env var. Byte fields are
written as arithmetic with a unit comment; every var goes in
`.env.example` under a `# ── RFP Ingestion sandbox ──` banner (the flag line
live as `RFP_INGESTION_ENABLED=false` with the `RFP_INGEST_ENABLED` alias
named in the comment, the others commented at their default);
`tests/conftest.py` pins the flag on through the alias
(`RFP_INGEST_ENABLED=true`).

---

## 5. Tests

- `tests/test_rfp_sandbox_child.py`: the child as a real subprocess on
  generated fixtures (pypdf and hand-written PDF bytes; reportlab is not
  installed): blank multi-page, text page (hand-written Helvetica content
  stream), encrypted, owner-only restricted, zero pages, truncated, giant
  media box, over-the-page-cap file, JavaScript + OpenAction + Launch + URI +
  embedded file, Flate bomb (20 s parent deadline; memory assertions
  `skipif(sys.platform != "linux")`), skip-file resume, verify mode, and the
  import-boundary test (fresh interpreter, `sys.modules` must contain nothing
  starting with `app.core`, `app.services`, `pydantic`, `supabase`, `httpx`).
- `tests/test_rfp_sandbox_runner.py`: progress parsing, torn trailing line,
  every validation rejection (bad JSON, NaN, deep nesting, unknown event,
  wrong sha, wrong dims, extra file, symlinked file, symlinked subdir, FIFO,
  `..` in a name, non-JPEG bytes, disallowed JPEG marker, trailing bytes,
  index out of range, duplicate index, oversized artifact), crash before
  ready / before document / mid-page, stall, open timeout, restart cap,
  disk quota, file timeout, run page budget kill, cancel mid-child, lease
  lost mid-child (child killed, no writes), stderr tail, cleanup; and the
  `page_sink` contract (delivery order, no bytes left on the result, pages
  that verify dropped are never delivered, no delivery at all for a result
  that is not `complete`, a digest that changed between validation and
  delivery failing the file `invalid_output`, a sink exception propagating
  with the work dir still removed, and the no-sink path still carrying bytes).
- `tests/test_rfp_sanitize.py`: sniff table (pdf, late header, junk before
  header, zip polyglot at 0, MZ at 0, MZ inside the first KB of a real PDF
  must pass, GIF/PDF polyglot, trailing ZIP, html, ps, empty, no EOF flag),
  the office table of 2.1 (OOXML and OLE2 by magic + extension, a `PK` file
  named `.pdf` stays `not_pdf`, a `.docx` name over PDF bytes is a PDF,
  families must agree, offset 0 only, no filename = the PDF-only table),
  JPEG marker walk, text sanitizer and hazard counts, and the child/parent
  cross-check: `app.sandbox.textclean.sanitize` and `sanitize_text` must agree
  exactly on a generated corpus covering every Unicode general category.
- `tests/test_rfp_office_convert.py`: the Gotenberg client over
  `httpx.MockTransport`: the one part named `source.<ext>`, no spreadsheet
  option, no redirects, streamed body under the cap with the digest, 5xx /
  timeout / unreachable -> unavailable, 4xx / empty -> rejected, over the
  cap -> too large, `dest` never left behind.
- `tests/test_rfp_ingest_storage.py`: path builders (incl. the format
  extension and `converted_path`), `delete_prefix(keep=...)`, migration
  0125 read as text, upload retry policy,
  streaming download (cap, 404, partial unlink), recursive delete, signed-url
  memo, `graph_stream` and `download_attachment_to_file` over
  `httpx.MockTransport`; plus migration 0121 read as text (both millisecond
  columns widened under the information_schema guard, the dropped and added
  indexes, the prune predicate derived from `RUN_TERMINAL_STATUSES`, and the
  house style of the file itself), since none of that can be exercised
  without a database.
- `tests/test_rfp_image_pdf.py`: parts split at the cap, output opens in
  pypdf with the right page count and media boxes.
- `tests/test_rfp_ingest.py`: FakeDB runner tests with `run_sandbox` faked;
  `_mark` CAS rules (sticky canceled, once-only bell on every terminal edge);
  status derivation incl. all-rejected and orphaned; gap threshold floor;
  duplicate via 23505; cancel between files and mid-child; lease lost;
  shutdown requeue; retry ordering (409 on active job); delete fencing;
  email materialization (Graph stream faked with `httpx.MockTransport`,
  404 -> not_stored, cap -> too_large); prune; and the sink path through
  `execute` (every upload batch renews the lease and re-checks the abort
  flag, a cancel mid-delivery stops uploading, and a verdict that lands after
  a delivery deletes the objects that delivery already wrote); a
  `SlotWaitTimeout` requeueing the run instead of burning the file; and
  `file_urls` keeping an unsignable images part in place as null so the part
  numbering cannot shift under the caller; and the office path of 2.1
  (Gotenberg on `httpx.MockTransport`): the upload records the format and
  the quarantine extension, the runner converts, sniffs, uploads
  `converted.pdf` and hands the child the PDF, the three failure verdicts,
  the re-run reuse (digest match only) and the cleanups that spare
  `converted.pdf`, the email attachment path, and the setting off.
- `tests/test_rfp_ingest_router.py`: route introspection (flag gate,
  `require_dev` and a rate limiter on every route), upload verdicts and 409s
  (office uploads included; the file detail exposes `source_format` and
  `conversion` and never signs quarantine or `converted.pdf`),
  staging-only uploads, start rules, dispatch to queue vs BackgroundTasks,
  cancel/retry/delete state rules, `q` sanitization.
- `tests/test_llm_queue.py` additions: `job_types` claim filter, two-pass
  claim capacity, `renew_lease` stale-attempt returns False, `_execute`
  against a non-LLM feature reaches `failed`, `requeue_self`.

Run: `cd bdr_be && uv run pytest -q` and `uv run ruff check .`;
frontend: `cd bdr_fe && npm run lint && npx tsc --noEmit && npm run build`
(`next build` no longer runs ESLint, so `npm run lint` is the real gate).

Real-file smoke, no database: `uv run python scripts/rfp_sandbox_smoke.py
[DIR]` runs `run_sandbox` with the Settings-derived limits over every PDF
under a directory (default `../subs`), prints a verdict table (status/code,
pages, ok/failed, restarts, elapsed, thumb/full bytes, bounds, peak RSS,
stderr tail) and the pypdf-verified images parts, and exits 1 on any
`failed/invalid_output` or `failed/spawn` (a bug in the child, the sanitizer
or the runner). 2026-09-09 baseline: 66 vendor spec sheets, all `complete`,
0 failed pages, 0 restarts, 16.7 s.

---

## 6. Railway operations

Rollout order (the order is load-bearing):

1. Apply migration 0119 (dev first, then staging, then prod at release time
   with approval). The queue worker in this code always names `job_types`
   when claiming, which needs 0119's 4-parameter `claim_llm_jobs`; deploying
   the code first leaves every LLM claim 404ing at PostgREST until the
   migration lands. Apply 0121 with it (the four schema corrections listed in
   3.6): the bigint widenings are what keep a long render from losing a whole
   file to a 22003. Both are idempotent and end with a PostgREST reload.
2. Deploy with `RFP_INGESTION_ENABLED=false` (the default; the older
   `RFP_INGEST_ENABLED` spelling is still read as an alias, so either name
   works, but set the canonical one and do not set both). Every `/rfp-ingest`
   route answers 404, the page is hidden, no sandbox pass runs in the queue.
3. Before flipping anything, confirm `RFP_EMAIL_INGESTION_INBOXES_ALLOWED` is
   empty in that environment. The flag is the master switch for both RFP
   Ingestion slices: with mailboxes listed it also starts the email intake's
   Graph polling loop (`docs/RFP_EMAIL_INGESTION.md`), which is not what
   "flip it and watch the self-test" is meant to test.
4. Flip the flag on staging, watch the boot log for
   `rfp ingest self-test ok in N ms (uid switch applied)` (or the loud
   `RFP INGEST SELF-TEST FAILED` line) and confirm
   `GET /rfp-ingest/status?self_test=1` (the plain route serves a cached
   report and spawns nothing)
   reports `self_test.ok`, `platform.uid_switch` true and all six
   `limits_applied` flags true, then flip prod the same way.

Office files (2.1): the conversion goes to `GOTENBERG_URL`, the same service
the previews and RFQ sends already need (the boot guard refuses production
without it). Two knobs on that service matter here: its `--api-timeout`
(30 s by default) must be at least `RFP_INGEST_OFFICE_CONVERT_TIMEOUT_SECONDS`
or a long LibreOffice run comes back 503 and the file lands
`failed/conversion_unavailable` (retryable, so the run can be retried once
the timeout is raised), and its request body limit must admit
`RFP_INGEST_MAX_FILE_BYTES`. A converter outage never fails a PDF file;
`RFP_INGEST_OFFICE_FILES_ENABLED=false` turns office files back into
`rejected/not_pdf` without a deploy.

Network egress: run the Gotenberg service with NO outbound network (no
internet, no route to the database, storage or the other private services;
only the API needs to reach it). LibreOffice imports attacker-controlled
documents, and although the pre-conversion scan (2.1) refuses `.docx` /
`.xlsx` containers with external links, embedded objects or DDE / INCLUDE
fields, the legacy `.doc` / `.xls` binaries cannot be scanned that way. A
converter with no egress turns any fetch LibreOffice might attempt into a
harmless failure. On Railway that means a private-networking-only service
with public networking off; locally, `docker run --network` an internal
network that only the API joins.

uid and rlimits: the container runs as root, so the switch is in force with
the defaults (`RFP_INGEST_SANDBOX_UID=65534`, pool base 60100, size 4); the
self-test takes uid 60104, so uids 60100..60104 must stay free of other
services. On Linux all six rlimits apply for a non-root child; `RLIMIT_AS`
is what makes a memory bomb a `memory` page failure instead of an OOM kill of
the worker. The image needs nothing beyond pypdfium2 and Pillow (both in the
lockfile); `sys.executable` is `/app/.venv/bin/python`.

Scratch and disk: with `RFP_INGEST_SCRATCH_DIR` empty the scratch root is
the system temp dir on the ephemeral disk; the runner refuses to start a file
unless `free - (file size or the per-file cap) - disk_mb` stays above
`scratch_reserve_mb` (2 GB) and marks it `failed/storage` otherwise, so an
undersized disk shows up as that verdict, not as a crash. Each running file
needs up to `sandbox_disk_mb` (3 GB) plus its source. Prefer a volume mount
once one exists (slot lock files and the 0o711 work dirs live there; the
root should not be world-writable).

Concurrency: `rfp_ingest_sandbox_concurrency` is per uvicorn worker, so the
2-worker container runs up to 2 children: budget 2 x `memory_mb` of child
address space, plus each parent's own working set. The parent side is small
and, importantly, does NOT scale with the page count: it streams one page of
image bytes at a time into an upload batch bounded by BOTH 32 pages and 96 MB
(section 2, image delivery), whichever comes first, which is tens of MB at
production tiers. Page text does not scale with the file either: the runner
spends one 64 MB `max_text_bytes_per_file` budget across the whole file in
page order and every page past it comes back empty, and the same cap bounds
`text.json`. Any single page is capped at `max_text_chars_per_page`.

A stuck run: read the row per phase, because only the sandbox phases have a
child heartbeating behind them.

- `phase = sandbox` or `verify`: `phase`, `pages_done`, `last_progress_at`
  and `child_pid` refresh at most every 10 s while the child is alive. Here,
  and only here, a `running` file whose `last_progress_at` is older than the
  page stall (120 s) plus the tick means the PARENT is wedged, not the child
  (the child's own stall is killed and respawned by the parent).
- `phase = upload`: no child exists. The row is touched at the phase change
  and then once per upload batch (32 pages), so `last_progress_at` keeps
  moving, but `pages_done` does not move again until the terminal CAS and
  `child_pid` is the LAST KNOWN pid of a process that has already exited.
  Never chase that pid.
- `phase = materialize`: one write at the phase change and nothing more until
  the sandbox starts, so a 450 MB Graph download legitimately looks still for
  minutes. A stale `last_progress_at` here is not evidence of anything.

In the two quiet phases the queue lease is the real liveness signal: the job
row's `lease_expires_at` (900 s, renewed every 30 s by the monitor and at
every upload flush) plus the worker log, not the file row.

A redeploy mid-file sets `SHUTTING_DOWN`, the child is killed, the file stays
`running` and the job is requeued as `interrupted` for the next worker; if
the worker died without that, the queue lease (900 s) expires and the sweep
requeues it, and the next
`execute` resets stale `running` files first. Cancel always wins: the run
CAS is sticky and the runner hands its file back on the next tick.

Memory: peak child RSS on real files (macOS, 2026-09-09 smoke): 80 to 150 MB
for letter spec sheets, 340 MB for a 475-page spec book, 650 to 700 MB for 36
to 40 sheet drawing sets at the 4000 px tier, and 1.18 GB for a 39 MB
three-sheet 30 x 42 in fire-sprinkler set. `RLIMIT_AS` (Linux only) bounds
VIRTUAL size, which runs above RSS, which is why the default is
`RFP_INGEST_SANDBOX_MEMORY_MB=3072` and not the 1536 MB this doc first
shipped: at 1536 the heaviest measured set trips the limit, and on a 1 to 5
page set three `memory` gaps already pass the gap allowance
(`max(2, ceil(0.05 x pages))`), so the whole file came back
`rejected/too_many_failed_pages` with every image dropped and no `/retry`
path. A page that trips the limit is `failed/memory` (a gap, respawned past),
never a crash of the worker. Watch `pages_failed` with code `memory` in the
first production runs and raise it further if it appears. Read that per
verdict: a `verified_with_gaps` file carries the counts on the row and one
`rfp_ingest_pages` row per page with its `code`, while a file that went over
the gap threshold is `rejected/too_many_failed_pages` and writes no page rows
at all, so its `memory` count is only in `manifest.verify` (`pages_failed`,
`failed_pages`). The floor the validator enforces is 512 MB. Container sizing: 2 children x 3 GB of address
space is virtual, not resident, so size the container on the measured RSS
figures above plus each parent's streaming working set, not on 2 x
`memory_mb`.

Storage cost (measured 2026-09-09): letter spec sheets about 200 KB thumb +
400 KB full + text per page (a 475-page spec book: 76 MB thumb, 158 MB full,
one 76 MB images part); 30 x 42 in drawing sheets at the 4000 px tier about
170 KB thumb + 1 MB full per page (a 36-sheet set: 6.7 MB thumb, 36.7 MB
full). The thumb-tier images PDF adds the thumb bytes again, plus the
quarantine copy of the source. `rfp_ingest_retention_days` (14) prunes both prefixes hourly and
marks the run `expired` (rows and manifests stay).
