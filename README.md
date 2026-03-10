# story2hard

Automatically syncs your [Storyteller](https://github.com/smoores-dev/storyteller) reading progress to [Hardcover](https://hardcover.app) every 15 minutes. One-way sync only with Storyteller as the source.

## Why

I use Storyteller as my self-hosted read-aloud server (synced epub3 + audiobook + ebooks). Hardcover is where I track and display my reading activity. My personal website pulls from the Hardcover API to show a live progress bar for whatever I'm currently reading. 

The problem: Storyteller and Hardcover don't talk to each other, so I always had to manually update my Hardcover progress. This container fixes that automatically.

---

## What it does

| Storyteller state | Hardcover action |
|---|---|
| Book has progress > 0% | Set to **Currently Reading** + sync % |
| Book reaches 100% | Set to **Read** + mark finished date |
| Book has 0% / never opened | Ignored |

Progress is synced using page-based tracking (the field Hardcover's API exposes as a percentage). Sync only fires when progress has changed by at least 1%.

---

## Setup on CasaOS

### Option A — Custom App (recommended)

1. Open CasaOS → **App Store** → **Custom Install** (the `+` button)
2. Paste the YAML below
3. Click **Submit**

```yaml
services:
  storyteller-hardcover-sync:
    image: storyteller-hardcover-sync:latest
    build:
      context: /opt/story2hard
    container_name: storyteller-hardcover-sync
    restart: unless-stopped
    network_mode: host
    environment:
      STORYTELLER_URL: "https://your-server.ts.net"
      STORYTELLER_USERNAME: "your_username"
      STORYTELLER_PASSWORD: "your_password"
      HARDCOVER_TOKEN: "your_hardcover_token"
      STATE_FILE: /data/state.json
      SYNC_INTERVAL_MINUTES: "15"
      MIN_PROGRESS_DELTA: "0.01"
    volumes:
      - sync-data:/data

volumes:
  sync-data:
```

> Replace the four credential values before submitting. See [Get your credentials](#get-your-credentials) below.

---

### Option B — Terminal

SSH into your server or open your linux client's terminal, then:

```bash
# Clone the repo
git clone https://github.com/reselling/story2hard.git /opt/story2hard
cd /opt/story2hard

# Create your env file
cp .env.example .env
nano .env          # fill in your four values

# Build and start
docker compose up -d --build
```

Watch the logs:

```bash
docker logs -f storyteller-hardcover-sync
```

---

## Credentials needed

| Variable | Where to find it |
|---|---|
| `STORYTELLER_URL` | The URL you use to access Storyteller |
| `STORYTELLER_USERNAME` | Your Storyteller login username |
| `STORYTELLER_PASSWORD` | Your Storyteller login password |
| `HARDCOVER_TOKEN` | [hardcover.app/account/api](https://hardcover.app/account/api) → generate a token, paste without the `Bearer ` prefix |

---

## Updating (if I do update)

```bash
cd /opt/story2hard
git pull
docker compose up -d --build
```

---

## Optional settings

| Variable | Default | Description |
|---|---|---|
| `SYNC_INTERVAL_MINUTES` | `15` | How often to check for progress changes |
| `MIN_PROGRESS_DELTA` | `0.01` | Minimum change (1%) before syncing |

---

## Troubleshooting

**"Could not find X on Hardcover"**
The book title in Storyteller doesn't match Hardcover's search. Rename the book in Storyteller to match exactly, then delete its entry from the Docker volume's `state.json` so it re-discovers it.

**Progress shows but percentage is 0% on Hardcover**
Make sure the book has an edition with a page count set in Hardcover. The sync links to whatever edition has the most pages.

**Container keeps restarting**
Check logs with `docker logs storyteller-hardcover-sync`. Usually a missing or wrong credential.
