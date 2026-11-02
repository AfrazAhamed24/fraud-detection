# TRACE-X

TRACE-X is a Flask application with a Three.js frontend for exploring transaction
networks and rule-based fraud-risk indicators.

## Project layout

```text
trace - x/
├── backend/
│   ├── app.py
│   └── requirements.txt
├── frontend/
│   ├── index.html
│   ├── config.js
│   └── vendor/
│       ├── OrbitControls.js
│       └── three.module.js
└── docs/
    ├── Conference Paper.docx
    └── System Architecture.pdf
```

## Run locally

From the project root:

```powershell
cd "C:\Users\afraz ahamed k m\Downloads\trace - x"
python -m pip install -r backend\requirements.txt
python backend\app.py
```

Open <http://127.0.0.1:5000/>.

The frontend currently loads Three.js from the jsDelivr CDN. The local copies in
`frontend/vendor` are retained for offline/vendor deployment options.

## Deployment

Set a production secret before deployment:

```powershell
$env:TRACE_X_SECRET_KEY = "replace-with-a-long-random-secret"
```

Use a production WSGI server rather than Flask's development server. For
example, after installing a WSGI server:

```powershell
waitress-serve --listen=*:8000 backend.app:app
```

Uploaded datasets are held in memory and isolated by Flask session cookie.
They are not persisted across process restarts or shared across workers.

For separate frontend/backend hosting, set `window.TRACE_X_API_BASE` in
`frontend/config.js` to the Render backend URL and set the Render environment
variable `FRONTEND_ORIGIN` to the exact Vercel URL.
