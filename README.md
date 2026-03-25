# racsview

Local viewer for RACS HiPS images and source catalogues.

## Setup

Install Python dependencies with `uv`:

```bash
uv sync
```

If you prefer `pip`, install from the project metadata:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

## Run

Start the viewer with:

```bash
./deploy.sh
```

This starts the local server on `http://localhost:8000/` and opens the browser.

## Datastore

Source catalogues are discovered recursively under a datastore directory. You can provide that directory in either of these ways:

1. Set `RACSVIEW_DATASTORE` before starting the server:

```bash
export RACSVIEW_DATASTORE=/path/to/datastore
./deploy.sh
```

2. Leave it unset and click `Set datastore` in the UI, then choose the directory in the native folder picker.

If the datastore is not configured, image viewing still works, but source catalogue loading stays disabled until a datastore directory is selected.
