"""The RFP Ingestion Sandbox: an out-of-process, credential-free PDF processor.

Everything under this package runs INSIDE the sandbox child, or is shared
with the parent as a pure contract (protocol.py). Hard rule, enforced by
tests/test_rfp_sandbox_child.py: nothing here imports app.core, app.services,
app.routers or any third-party package other than pypdfium2 and Pillow. The
child holds no credentials, opens no sockets, and communicates with the
parent only through the output directory it was handed.

See docs/RFP_INGESTION_SANDBOX.md for the design and the trust boundary.
"""
