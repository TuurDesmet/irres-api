# Scripts

This directory contains utility scripts for the Irres API project.

## botpress-sync

A daily synchronization script that syncs data from the Irres Listings API to a Botpress chatbot.

### Features

- Syncs listing data with property details and images
- Syncs office images
- Syncs location filters
- Validates data before updating Botpress tables
- Configurable timeouts for slow API endpoints

### Usage

```bash
cd botpress-sync
pip install -r requirements.txt
python sync_botpress.py
```

### Environment Variables

The script requires two environment variables:

- `BOT_ID`: Your Botpress Bot ID
- `BOTPRESS_TOKEN`: Your Botpress API token

### How It Works

1. Fetches data from three API endpoints:
   - `/api/listings` - Property listings with details
   - `/api/office-images` - Office photos
   - `/api/locations` - Location filters

2. Validates responses before syncing to prevent data loss

3. Updates three Botpress tables:
   - `ListingsTable`
   - `OfficeImagesTable`
   - `FilterLocationsTable`

### GitHub Actions

This script runs automatically via GitHub Actions every day at midnight UTC. Configuration is in [`.github/workflows/daily_sync.yml`](.github/workflows/daily_sync.yml).
