"""The live dashboard (``scio dashboard``, issue #68, ADR 0017), read-only but for one write:
the owner's answer to a parked feature's question (``POST /api/answer``, token-protected,
ADR 0020), which posts an ``answer:`` comment on the feature's issue and writes the workspace's
answer file, and never writes a checkpoint.

:mod:`scio.dashboard.contract` is what every layer speaks; :mod:`scio.dashboard.data` reads the
workspaces' checkpoint DBs, cost ledgers and ``progress.json`` without writing anything;
:mod:`scio.dashboard.app` serves them as JSON and a WebSocket; ``static/`` is the page. The
server needs the optional ``dashboard`` extra (FastAPI and uvicorn); the contract and the data
layer need only SCIO's core dependencies.
"""
