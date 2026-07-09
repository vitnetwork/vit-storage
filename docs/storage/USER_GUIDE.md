# VIT Storage User Portal Guide

## 1. Quick Start
To get started with the portal:
1. Start the VIT Storage backend server.
2. Open your web browser and navigate to:
   `http://localhost:8080/dashboard`
3. The modern **Swarm Coordination Portal** will load, pre-configured in dark mode.

---

## 2. Navigating the Portal

### 2.1 The Dashboard
The **Dashboard** is your operations center. It highlights:
- **Metrics Summary**: Live count of files managed, space consumed, Reed-Solomon config ratio, and active cloud nodes.
- **Provider Status**: Direct link and ping latency monitoring for each registered cloud provider.
- **Continuous Shard Monitor**: Status of automated background integrity audits.

### 2.2 The File Manager ("My Files")
Use the **My Files** workspace to organize your decentralised archive:
- **Uploading**: Simply drop folders or individual files directly into the dashed dropzone, or click to use the native file selector. Upload progress is shown with speed indicators.
- **Deletes & Deletions**: Click the trash icon to trigger coordinate delete.
- **Downloads**: Reassembles and streams your reconstructed files instantly from multi-cloud fragments.
- **Bulk Actions**: Select multiple files via checking left-hand inputs and click "Bulk Delete" to clean up multiple sets.

### 2.3 Shared Files Workspace
View all current secure download links generated. You can:
- **Copy Link**: Copies the link to your clipboard.
- **Revoke Link**: Instantly blocks any future access to that sharing path.

---

## 3. Advanced Features

### 3.1 Inline File Previews
Clicking the eye icon on any supported file launches the media preview container. Images, Text files, PDFs, Audio, and Video files can be previewed directly without initiating a complete physical download of the asset.

### 3.2 Secure Expiry Links
When sharing a file, click the share icon. You can:
- Standardize expiration (1 Hour, 24 Hours, 7 Days, or Never).
- Password protect the file.
- Enforce strict download limits (e.g. max 5 downloads).
- Enforce Read-Only views.
