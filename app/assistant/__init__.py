"""The per-user BDR Assistant (docs/ASSISTANT.md).

The assistant is one more client of the FastAPI app: every tool is a mapping
to a real HTTP route, executed in-process through httpx.ASGITransport over
the shim in `shim.py` under the server-minted `bdra_` principal in
`principal.py`, inside the firewall in `firewall.py`. Nothing in this package
reads or writes Supabase on a tool's behalf; the one database read in the
package is the delegation check in `principal.py`, which is authorization
itself rather than a tool. `app.main` never imports this package.
"""
