# The Pet Foundry — Inventory Management System

A full-featured, GS1-compliant inventory management system with POS, barcode scanning, multi-warehouse support, purchase/sales orders, invoicing, and AI-powered insights via Ollama.

---

## 🚀 Quick Start

### Prerequisites
- Python 3.9+ (with pip)
- Optional: [Ollama](https://ollama.com) for AI features

### 1. Clone & Install

```bash
git clone https://github.com/yourusername/thepetfoundry-inventory.git
cd thepetfoundry-inventory
pip install -r requirements.txt
```

### 2. Run

```bash
python app.py
```

Open [http://localhost:5050](http://localhost:5050) — on first run it creates the admin account automatically.

### 3. (Optional) Enable AI Features

```bash
# Install Ollama, then:
ollama pull llama3
ollama serve
```

AI features will auto-detect Ollama at `http://localhost:11434`.

---

## 🤖 Ollama AI Features

The app integrates with [Ollama](https://ollama.com) for local, privacy-preserving AI:

| Feature | Description |
|---|---|
| **AI Quick Insight** | Dashboard card with restocking recommendation |
| **AI Product Description** | Auto-generate product copy from name + category |
| **AI Stock Report** | Full analyst report on low/out-of-stock items |
| **AI Smart Search** | Natural language product search |

### Ollama Settings

Navigate to **Settings → Ollama AI** to configure:
- **Base URL** — where Ollama is running (default: `http://localhost:11434`)
- **Model** — e.g. `llama3`, `mistral`, `phi3`

### Setting up Ollama

```bash
# macOS/Linux
curl -fsSL https://ollama.com/install.sh | sh

# Pull a model
ollama pull llama3

# Start Ollama (runs in background)
ollama serve
```

---

## 🌐 Deployment

### Option A: Self-Hosting (VPS, Railway, Render)

Deploy directly on any Python-capable platform:

```bash
# Set environment variables
export SECRET_KEY=your-random-secret
export FLASK_ENV=production

# Run
python app.py
```

Or use Docker:
```bash
docker-compose up -d
```

### Option B: Netlify (Static + Self-Hosted Backend)

The repository includes a **`netlify-frontend/`** folder — a pure static SPA that calls your self-hosted API.

1. Deploy the Flask backend on Railway/Render (or your VPS)
2. Set `API_BASE_URL` to your backend URL in `netlify-frontend/`
3. Drag `netlify-frontend/` to [Netlify Drop](https://app.netlify.com/drop)

---

## 📁 Folder Structure

```
thepetfoundry-inventory/
├── app.py                  # Main Flask application
├── _patch_auth.py          # Auth route patcher (run once)
├── requirements.txt        # Python dependencies
├── Dockerfile             # Container build
├── docker-compose.yml     # Dev setup with Ollama
├── .env.example            # Environment variables template
├── .gitignore
├── tpims.db                # SQLite database (auto-created)
├── static/
│   └── barcodes/           # Generated barcode images
├── exports/                # Exported reports/CSVs
├── templates/
│   ├── base.html           # Shared layout + sidebar
│   ├── dashboard.html      # Dashboard + AI insight
│   ├── auth/
│   │   └── login.html
│   ├── products/
│   │   └── list.html
│   ├── scan.html
│   └── settings/
│       ├── index.html
│       └── ollama.html     # Ollama AI settings
└── netlify-frontend/
    └── index.html          # Static SPA for Netlify deployment
```

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | auto-generated | Flask session secret |
| `DATABASE` | `tpims.db` | SQLite database path |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `llama3` | Ollama model name |
| `FLASK_ENV` | `development` | `production` or `development` |

---

## ✨ Features

- **GS1-compliant barcode generation** (GS1-128, QR, EAN-13, CODE-128/39)
- **Multi-warehouse inventory** with transfers and adjustments
- **Serial & batch tracking** for warranty/lot management
- **Purchase Orders & Sales Orders** with GST invoicing
- **POS Quick Sale** screen with barcode scanner support
- **Low stock email alerts** via Brevo/Gmail/SMTP
- **Cycle counts** for physical inventory verification
- **API-first design** — full REST API for integrations
- **AI features** via Ollama (runs locally, no cloud needed)

---

## 🔐 First-Time Setup

On first run, the app creates:
- Default **admin user** (any email/password you choose on signup)
- Default **warehouse** ("Main Warehouse")
- Default **units** (pcs, kg, box, m, L, set, dozen, g, m²)
- Default **category** (General)

---

## 📄 License

MIT — free for personal and commercial use.
