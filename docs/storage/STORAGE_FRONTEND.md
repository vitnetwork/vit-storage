# VIT Storage Frontend Architecture

## 1. Overview
The VIT Storage User Interface (UI) is built as a highly performant, responsive, self-contained **Single-Page Application (SPA)**. To preserve production stability and avoid introducing heavy Node.js compiler layers or custom build pipelines, the frontend uses a direct-to-CDN modern stack:
- **Tailwind CSS**: Utility-first CSS framework for highly-customizable UI design, supporting dynamic light/dark mode configuration.
- **Alpine.js**: Lightweight JavaScript reactive framework to bind variables, track download/upload queues, and manage UI state.
- **Lucide Icons**: Beautiful open-source vector icon set loaded dynamically.
- **FastAPI Static Mounts**: Served directly from Python with zero additional process dependencies.

---

## 2. Directory Structure & Mounting Configuration
The frontend code is structured cleanly inside an independent folder to allow future migrations or standalone static hosting:
```text
frontend/
└── static/
    └── index.html      # Self-contained Single Page Application
```

### 2.1 Backend Router Binding
In `main.py`, the frontend assets are mounted and served via:
```python
from fastapi.staticfiles import StaticFiles

# Mount static files directory
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

# Dashboard UI Route
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return FileResponse("frontend/static/index.html")
```

---

## 3. UI Features & Capabilities

### 3.1 Adaptive Dark Mode
The interface automatically initializes with system/user theme preferences. Switching themes toggles the `dark` class on the root HTML element, updating all design variables instantly.

### 3.2 Live Swarm Uploads
- **Drag-and-Drop Zone**: Users can drop any file directly onto the dashboard to trigger multi-cloud parallel Reed-Solomon coding fragmentation.
- **Progress Tracking**: Tracks upload speed (e.g. `4.2 MB/s`), estimated time remaining (ETA), progress percentage, and supports cancel actions.

### 3.3 Dynamic Inline Previews
Previews of files load in real-time from the multi-cloud pool without forcing a complete physical download of the asset. It supports:
- **Images**: Rendered inside dynamic containers with adaptive sizing.
- **PDF Documents**: Framed in sandboxed viewports.
- **Text / JSON / Code**: Code blocks are parsed and styled with high-contrast text views.
- **Audio / Video**: Streamed directly into standard native players.

### 3.4 Keyboard Shortcuts
Super-fast navigation across tabs is mapped to:
- `g d` - Navigate to Dashboard
- `g f` - Navigate to My Files File Manager
- `g p` - Navigate to API Playground
- `u` - Select and upload files
- `?` - Open shortcuts cheat sheet
- `esc` - Dismiss modal overlays
